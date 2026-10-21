"""Backups (tech-design §9.2): `VACUUM INTO` a timestamped file — atomic, consistent, no external tooling.

Taken daily (a `backup` job the scheduler queues when the newest backup is a day old, so a laptop that sleeps
still gets one), before every migration (`python -m hisaab backup --before-migrating`, run by scripts/run.sh
only when the database is behind), and before every re-sync (the re-sync endpoint, before it queues anything).

    <backup_dir>/hisaab-2026-10-21T073000Z-daily.db      (also -pre-migration, -pre-resync, -manual)

The folder is HISAAB_BACKUP_DIR (default data/backups); §9.2 says it should point at synced or external
storage, and unlike the live database a finished backup may live on the Windows mount. The newest KEEP
backups are kept, of every kind; only files with that name pattern are ever deleted, and only after the new
backup is complete.

A backup is a read transaction under WAL: it neither waits for a writer nor blocks one, and it copies the last
committed state. Restore is manual and documented in OPERATIONS.md (stop, replace the file, start): automating
it risks doing it by accident.
"""

import logging
import os
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory

from hisaab.config import PROJECT_ROOT, Settings
from hisaab.jobs.queue import JobContext, JobQueue

_log: logging.Logger = logging.getLogger(__name__)
KEEP: int = 30
DAILY_EVERY: timedelta = timedelta(hours=24)
_NAME: re.Pattern[str] = re.compile(r"^hisaab-(\d{4}-\d{2}-\d{2}T\d{6}Z)-[a-z0-9-]+\.db$")


class BackupFailed(RuntimeError):
    pass


def backup(db_path: Path, folder: Path, reason: str, now: datetime | None = None) -> Path:
    """A consistent copy of the database in `folder`, then the folder pruned to the newest KEEP backups."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise BackupFailed(f"can't use the backup folder {folder}: {e.strerror or e}") from None
    stamp: str = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    target: Path = folder / f"hisaab-{stamp}-{reason}.db"
    n: int = 1
    while target.exists():  # two in one second: never overwrite (VACUUM INTO refuses to anyway)
        n += 1
        target = folder / f"hisaab-{stamp}-{reason}-{n}.db"
    # Written under a name that is no backup's, made durable, then renamed: a kill or power loss mid-way leaves
    # only a hidden .partial file, never a truncated file that would pass as the newest backup.
    partial: Path = folder / f".{target.name}.partial"
    partial.unlink(missing_ok=True)
    try:
        with closing(sqlite3.connect(_read_only(db_path), uri=True, timeout=30)) as c:
            c.execute("VACUUM INTO ?", (str(partial),))
        _fsync(partial)
        os.replace(partial, target)
        _fsync(folder)
    except (sqlite3.Error, OSError) as e:
        partial.unlink(missing_ok=True)
        raise BackupFailed(f"couldn't back up {db_path} to {folder}: {e}") from None
    prune(folder)
    return target


def _fsync(path: Path) -> None:
    """VACUUM INTO doesn't sync its output (SQLite docs): flush the file, or the folder after a rename."""
    fd: int = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_only(db_path: Path) -> str:
    """A read-only URI with the path percent-encoded: a '#' or '?' in it would otherwise end the path, and SQLite
    would open (and create) a different, empty file."""
    return f"{db_path.resolve().as_uri()}?mode=ro"


def backups(folder: Path) -> list[Path]:
    """Every backup in the folder, newest first: by the timestamp in its name, then (two in one second) by when
    the file was written."""
    if not folder.is_dir():
        return []
    found: list[Path] = [p for p in folder.iterdir() if _NAME.match(p.name)]
    return sorted(found, key=lambda p: (taken_at(p), p.stat().st_mtime_ns), reverse=True)


def prune(folder: Path, keep: int = KEEP) -> list[Path]:
    """Delete all but the newest `keep`. A file that can't be deleted (locked on the Windows side, say) is logged
    and left: the new backup is already safe, and refusing a start or a re-sync over it would help nobody."""
    gone: list[Path] = []
    for path in backups(folder)[keep:]:
        try:
            path.unlink(missing_ok=True)
            gone.append(path)
        except OSError as e:
            _log.warning("couldn't delete old backup %s: %s", path, e)
    return gone


def latest(folder: Path) -> Path | None:
    found: list[Path] = backups(folder)
    return found[0] if found else None


def taken_at(path: Path) -> datetime:
    m: re.Match[str] | None = _NAME.match(path.name)
    assert m is not None, path
    return datetime.strptime(m.group(1), "%Y-%m-%dT%H%M%SZ").replace(tzinfo=UTC)


def current_revision(db_path: Path) -> str | None:
    """The database's migration revision, or None for a missing or empty file."""
    if not db_path.exists():
        return None
    with closing(sqlite3.connect(_read_only(db_path), uri=True)) as c:
        try:
            row: Any = c.execute("SELECT version_num FROM alembic_version").fetchone()
        except sqlite3.OperationalError:  # no alembic_version table yet: an empty file
            return None
    return str(row[0]) if row else None


def migrations_pending(db_path: Path) -> bool:
    """Whether the database exists at a revision this code knows and is behind the newest migration. A fresh
    install has nothing to save; a database AHEAD of this code (an older checkout) can't be migrated, so alembic
    refuses it, and backing it up on every refused start would only push good backups out."""
    revision: str | None = current_revision(db_path)
    if revision is None:
        return False
    script: ScriptDirectory = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))
    try:
        script.get_revision(revision)
    except Exception:  # alembic.util.CommandError / ResolutionError: a revision this code doesn't have
        return False
    return revision != script.get_current_head()


def pre_migration_reason(db_path: Path) -> str:
    """Named by the revision it protects, so a start that keeps failing (systemd retries it) takes one backup of
    that state, not one per try that would push the good backups out."""
    return f"pre-migration-from-{current_revision(db_path)}"


def make_backup(settings: Settings) -> Callable[[JobContext], dict[str, Any]]:
    def run(ctx: JobContext) -> dict[str, Any]:
        made: Path = backup(settings.database_path, settings.backup_dir, "daily")
        return {"file": made.name, "kept": len(backups(settings.backup_dir))}
    return run


def enqueue_due_backup(queue: JobQueue, folder: Path, now: datetime | None = None) -> str | None:
    """The daily schedule: a backup job when the newest backup is a day old, or there is none."""
    newest: Path | None = latest(folder)
    if newest is not None and (now or datetime.now(UTC)) - taken_at(newest) < DAILY_EVERY:
        return None
    return queue.enqueue("backup")


