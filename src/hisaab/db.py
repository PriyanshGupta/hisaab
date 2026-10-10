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
from pathlib import Path

from sqlalchemy import Engine, create_engine, event

# (name, value). foreign_keys is handled separately because migrations turn it off.
CONNECTION_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("journal_mode", "WAL"),  # readers never blocked by the writer
    ("busy_timeout", "5000"),  # wait for a lock rather than error
    ("synchronous", "NORMAL"),  # safe under WAL, materially faster
)

# Filesystems on which SQLite locking cannot be trusted. 9p/drvfs/v9fs are how
# WSL2 mounts Windows drives; the rest are network or FUSE (e.g. ntfs-3g) mounts.
UNSAFE_FILESYSTEMS = frozenset({"9p", "drvfs", "v9fs", "cifs", "smb3", "smbfs", "nfs", "nfs4", "fuseblk"})

# Belt and braces: WSL mounts drive letters at /mnt/<letter>, whatever /proc/mounts says.
_WINDOWS_DRIVE = re.compile(r"^/mnt/[a-zA-Z](/|$)")


class UnsafeDatabaseLocation(RuntimeError):
    pass


def _unescape_mount_field(field: str) -> str:
    # /proc/mounts encodes space, tab, newline and backslash as octal: "\040" etc.
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def filesystem_type(path: Path, mounts_text: str | None = None) -> str:
    """Filesystem type of the mount containing `path` (longest mount-point match)."""
    if mounts_text is None:
        mounts_text = Path("/proc/mounts").read_text()
    best_mount, best_type = None, "unknown"
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        mount_point = Path(_unescape_mount_field(fields[1]))
        if path.is_relative_to(mount_point) and (
            best_mount is None or len(mount_point.parts) > len(best_mount.parts)
        ):
            best_mount, best_type = mount_point, fields[2]
    return best_type


def assert_safe_db_location(db_path: Path, mounts_text: str | None = None) -> None:
    # resolve() follows symlinks, so /home/u/data -> /mnt/c/... is judged by its target.
    resolved = db_path.expanduser().resolve()
    fstype = filesystem_type(resolved, mounts_text)
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
    engine = create_engine(f"sqlite:///{db_path}")
    foreign_keys = "ON" if enforce_foreign_keys else "OFF"

    @event.listens_for(engine, "connect")
    def _configure_connection(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            for name, value in CONNECTION_PRAGMAS:
                cursor.execute(f"PRAGMA {name} = {value}")
            cursor.execute(f"PRAGMA foreign_keys = {foreign_keys}")
        finally:
            cursor.close()

    return engine
