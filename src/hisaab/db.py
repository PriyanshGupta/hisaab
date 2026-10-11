"""SQLite engine construction.

Two jobs, both load-bearing:

1. Every new DBAPI connection gets the PRAGMAs from tech-design §3.1. Three of
   them (busy_timeout, synchronous, foreign_keys) are per-connection in SQLite
   and silently revert to defaults on a connection that was not configured;
   foreign_keys in particular defaults to OFF. So they are set in a "connect"
   event listener, which fires for every connection the pool ever opens.

2. The database file must never live on the Windows drive mount under WSL2
   (tech-design §2.3). SQLite's locking relies on POSIX file locks, which the
   9p/drvfs mount does not implement correctly. That is a corruption path, so
   make_engine refuses such a location before SQLite ever opens the file.

This is the only module (with migrations/env.py) allowed to issue SQLite-only
statements. Everything else stays dialect-neutral (tech-design §2.2).
"""

import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine.interfaces import DBAPIConnection, DBAPICursor
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

# (name, value). foreign_keys is handled separately because migrations turn it off.
CONNECTION_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("journal_mode", "WAL"),  # readers never blocked by the writer
    ("busy_timeout", "5000"),  # wait for a lock rather than error
    ("synchronous", "NORMAL"),  # safe under WAL, materially faster
)

# Filesystems on which SQLite locking cannot be trusted. 9p/drvfs/v9fs are how
# WSL2 mounts Windows drives; the rest are network or FUSE (e.g. ntfs-3g) mounts.
UNSAFE_FILESYSTEMS: frozenset[str] = frozenset(
    {"9p", "drvfs", "v9fs", "cifs", "smb3", "smbfs", "nfs", "nfs4", "fuseblk"})

# Belt and braces: WSL mounts drive letters at /mnt/<letter>, whatever /proc/mounts says.
_WINDOWS_DRIVE: re.Pattern[str] = re.compile(r"^/mnt/[a-zA-Z](/|$)")


class UnsafeDatabaseLocation(RuntimeError):
    pass


def _unescape_mount_field(field: str) -> str:
    # /proc/mounts encodes space, tab, newline and backslash as octal: "\040" etc.
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def filesystem_type(path: Path, mounts_text: str | None = None) -> str:
    """Filesystem type of the mount containing `path` (longest mount-point match)."""
    if mounts_text is None:
        mounts_text = Path("/proc/mounts").read_text()
    best_mount: Path | None = None
    best_type: str = "unknown"
    for line in mounts_text.splitlines():
        fields: list[str] = line.split()
        if len(fields) < 3:
            continue
        mount_point: Path = Path(_unescape_mount_field(fields[1]))
        if path.is_relative_to(mount_point) and (
            best_mount is None or len(mount_point.parts) > len(best_mount.parts)
        ):
            best_mount, best_type = mount_point, fields[2]
    return best_type


def assert_safe_db_location(db_path: Path, mounts_text: str | None = None) -> None:
    # resolve() follows symlinks, so /home/u/data -> /mnt/c/... is judged by its target.
    resolved: Path = db_path.expanduser().resolve()
    fstype: str = filesystem_type(resolved, mounts_text)
    if fstype in UNSAFE_FILESYSTEMS or _WINDOWS_DRIVE.match(str(resolved)):
        raise UnsafeDatabaseLocation(
            f"Refusing to use database at {resolved} (filesystem: {fstype}). "
            "SQLite locking is unreliable on the Windows mount and network filesystems; "
            "set HISAAB_DATABASE_PATH to a path on the Linux filesystem, e.g. under /home."
        )


def make_engine(db_path: Path, *, enforce_foreign_keys: bool = True) -> Engine:
    """Engine for the SQLite file at `db_path`, with every connection configured.

    enforce_foreign_keys=False exists for migrations only; see migrations/env.py.
    """
    assert_safe_db_location(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine: Engine = create_engine(f"sqlite:///{db_path}")
    foreign_keys: str = "ON" if enforce_foreign_keys else "OFF"

    @event.listens_for(engine, "connect")
    def _configure_connection(dbapi_connection: DBAPIConnection, _connection_record: ConnectionPoolEntry) -> None:
        cursor: DBAPICursor = dbapi_connection.cursor()
        try:
            for name, value in CONNECTION_PRAGMAS:
                cursor.execute(f"PRAGMA {name} = {value}")
            cursor.execute(f"PRAGMA foreign_keys = {foreign_keys}")
        finally:
            cursor.close()

    return engine


class WriterBusy(RuntimeError):
    """The writer lock could not be taken in time (e.g. a long reprocess holds it)."""


class Database:
    """The engine plus the process-wide single writer lock.

    Write-path ruling (2026-10-10, amends tech-design §6.7): every write, whether
    a background job or a user action like login or an edit, goes through
    write_session(), which holds `writer_lock` for the duration of one short
    transaction. So exactly one writer exists at a time by construction, and
    SQLite never has to arbitrate between two (its locking stays as a backstop).
    Jobs (slice 12) take the same lock, releasing it between batches; only a
    reprocess holds it for a whole run, which is what PRD §10.2 asks for.

    read_session() never commits. Slice 12 gives it a separate read-only pool.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine: Engine = engine
        self.writer_lock: threading.Lock = threading.Lock()
        self._sessions: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def read_session(self) -> Iterator[Session]:
        session: Session = self._sessions()
        try:
            yield session
        finally:
            session.rollback()
            session.close()

    @contextmanager
    def write_session(self, timeout: float = 10.0) -> Iterator[Session]:
        """Hold the writer lock for one transaction: commit on success, roll back on error."""
        if not self.writer_lock.acquire(timeout=timeout):
            raise WriterBusy(f"database writer busy for more than {timeout}s")
        try:
            with self._sessions.begin() as session:
                yield session
        finally:
            self.writer_lock.release()

    @contextmanager
    def try_write_session(self) -> Iterator[Session | None]:
        """Best-effort write: yields None at once if another writer holds the lock.

        For writes that may simply be skipped, like sliding a session's expiry,
        so a request never waits behind a long job.
        """
        if not self.writer_lock.acquire(blocking=False):
            yield None
            return
        try:
            with self._sessions.begin() as session:
                yield session
        finally:
            self.writer_lock.release()
