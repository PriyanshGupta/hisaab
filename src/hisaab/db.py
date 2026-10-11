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


def make_engine(db_path: Path, *, enforce_foreign_keys: bool = True, read_only: bool = False,
                pool_size: int | None = None) -> Engine:
    """Engine for the SQLite file at `db_path`, with every connection configured.

    enforce_foreign_keys=False exists for migrations only; see migrations/env.py.
    read_only=True opens every connection with SQLite's mode=ro: the read pool
    (tech-design §6.7). That is fixed when the file is opened, so no statement
    or PRAGMA run later on the connection can make it write.
    pool_size caps the pool with no overflow: 1 for the single write connection.
    """
    assert_safe_db_location(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    url: str = f"sqlite:///file:{db_path}?mode=ro&uri=true" if read_only else f"sqlite:///{db_path}"
    pool_args: dict[str, int] = {} if pool_size is None else {"pool_size": pool_size, "max_overflow": 0}
    engine: Engine = create_engine(url, **pool_args)
    foreign_keys: str = "ON" if enforce_foreign_keys else "OFF"
    # journal_mode is a property of the file, set by the writer; a read-only connection cannot set it.
    pragmas: tuple[tuple[str, str], ...] = CONNECTION_PRAGMAS[1:] if read_only else CONNECTION_PRAGMAS

    @event.listens_for(engine, "connect")
    def _configure_connection(dbapi_connection: DBAPIConnection, _connection_record: ConnectionPoolEntry) -> None:
        cursor: DBAPICursor = dbapi_connection.cursor()
        try:
            for name, value in pragmas:
                cursor.execute(f"PRAGMA {name} = {value}")
            cursor.execute(f"PRAGMA foreign_keys = {foreign_keys}")
        finally:
            cursor.close()

    return engine


class WriterBusy(RuntimeError):
    """The writer lock could not be taken in time (e.g. a long reprocess holds it)."""


class Database:
    """The two connection pools plus the process-wide single writer lock.

    Pools (tech-design §6.7): the write engine holds exactly one connection; the
    read engine's connections are opened read-only, so a handler that reads
    through read_session() physically cannot write.

    Write-path ruling (2026-10-10, amends tech-design §6.7): every write, whether
    a background job or a user action like login or an edit, goes through
    write_session(), which holds `writer_lock` for the duration of one
    transaction on the one write connection. So exactly one writer exists at a
    time by construction, and SQLite never has to arbitrate between two (its
    locking stays as a backstop). Jobs release the lock between batches; only a
    reprocess holds it for a whole run, which is what PRD §10.2 asks for.
    """

    def __init__(self, db_path: Path) -> None:
        self.write_engine: Engine = make_engine(db_path, pool_size=1)
        self.read_engine: Engine = make_engine(db_path, read_only=True)
        self.writer_lock: threading.Lock = threading.Lock()
        self._writes: sessionmaker[Session] = sessionmaker(bind=self.write_engine, expire_on_commit=False)
        self._reads: sessionmaker[Session] = sessionmaker(bind=self.read_engine)
        # The thread inside write_session and its DBAPI connection, for interrupt_writer.
        self._writer: tuple[int, DBAPIConnection] | None = None

    def dispose(self) -> None:
        self.write_engine.dispose()
        self.read_engine.dispose()

    @contextmanager
    def read_session(self) -> Iterator[Session]:
        session: Session = self._reads()
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
            yield from self._transaction()
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
            yield from self._transaction()
        finally:
            self.writer_lock.release()

    def _transaction(self) -> Iterator[Session]:
        with self._writes.begin() as session:
            dbapi: DBAPIConnection | None = session.connection().connection.dbapi_connection
            assert dbapi is not None  # a checked-out connection always has one
            self._writer = (threading.get_ident(), dbapi)
            try:
                yield session
            finally:
                self._writer = None

    def interrupt_writer(self, owner: int) -> None:
        """Abort the SQL statement now running on the write connection, if thread `owner` is
        the one writing (a job's watchdog must never abort a user's write). Its transaction
        then fails and rolls back."""
        writer: tuple[int, DBAPIConnection] | None = self._writer
        if writer is not None and writer[0] == owner:
            writer[1].interrupt()  # sqlite3.Connection.interrupt is thread-safe
