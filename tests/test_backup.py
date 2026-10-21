"""Backups (tech-design §9.2): VACUUM INTO a timestamped file, daily and before every migration and re-sync, the
newest 30 kept. A backup is a read transaction under WAL: it never blocks a writer and is always consistent."""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine

from hisaab import cli
from hisaab.config import Settings
from hisaab.db import Database
from hisaab.jobs.queue import JobContext
from hisaab.ops import backup as ops
from tests import factories as f
from tests.conftest import migrate

NOW: datetime = datetime(2026, 10, 21, 7, 30, tzinfo=UTC)


def rows(path: Path, table: str = "users") -> int:
    with sqlite3.connect(path) as c:
        count: int = c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return count


def test_a_backup_is_a_consistent_named_copy(settings: Settings, conn: Connection) -> None:
    f.user(conn)
    conn.commit()
    made: Path = ops.backup(settings.database_path, settings.backup_dir, "daily", now=NOW)
    assert made == settings.backup_dir / "hisaab-2026-10-21T073000Z-daily.db"
    with sqlite3.connect(made) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert rows(made) == 1


def test_a_backup_neither_waits_for_nor_blocks_an_open_write(settings: Settings, conn: Connection) -> None:
    f.user(conn)
    conn.commit()
    writer: sqlite3.Connection = sqlite3.connect(settings.database_path, timeout=0)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO settings (key, user_id, value, updated_at) VALUES ('x', '', '1', 'now')")
    made: Path = ops.backup(settings.database_path, settings.backup_dir, "daily", now=NOW)  # while the write is open
    writer.commit()
    writer.close()
    assert rows(made) == 1  # the committed state...
    with sqlite3.connect(made) as c:
        assert c.execute("SELECT count(*) FROM settings WHERE key = 'x'").fetchone()[0] == 0  # ...not the half-done write
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_thirty_are_kept_and_nothing_else_in_the_folder_is_touched(settings: Settings, migrated_engine: Engine) -> None:
    folder: Path = settings.backup_dir
    folder.mkdir(parents=True)
    (folder / "notes.txt").write_text("mine")  # there from the start: every prune sees it
    for i in range(31):
        ops.backup(settings.database_path, folder, "daily", now=NOW + timedelta(minutes=i))
    kept: list[str] = sorted(p.name for p in folder.iterdir())
    assert len([k for k in kept if k.startswith("hisaab-")]) == 30
    assert "hisaab-2026-10-21T073000Z-daily.db" not in kept and "notes.txt" in kept
    assert ops.latest(folder) == folder / "hisaab-2026-10-21T080000Z-daily.db"


def test_a_folder_that_cannot_be_made_fails_clearly_and_prunes_nothing(settings: Settings, migrated_engine: Engine,
                                                                        tmp_path: Path) -> None:
    blocker: Path = tmp_path / "a-file"
    blocker.write_text("not a folder")
    with pytest.raises(ops.BackupFailed, match="a-file"):
        ops.backup(settings.database_path, blocker / "backups", "daily", now=NOW)


def test_migrations_pending(settings: Settings, tmp_path: Path) -> None:
    assert ops.migrations_pending(tmp_path / "missing.db") is False
    migrate(settings.database_path, "0012")
    assert ops.migrations_pending(settings.database_path) is True
    migrate(settings.database_path)
    assert ops.migrations_pending(settings.database_path) is False


def test_the_cli_backs_up_before_migrating_only_when_behind(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    migrate(settings.database_path, "0012")
    assert cli.main(["backup", "--before-migrating"], settings=settings) == 0
    [made] = list(settings.backup_dir.iterdir())
    assert made.name.endswith("-pre-migration-from-0012.db")
    migrate(settings.database_path)
    assert cli.main(["backup", "--before-migrating"], settings=settings) == 0
    assert len(list(settings.backup_dir.iterdir())) == 1  # at head: nothing to protect against
    assert cli.main(["backup"], settings=settings) == 0
    assert cli.main(["backups"], settings=settings) == 0
    listing: list[str] = capsys.readouterr().out.strip().splitlines()
    assert listing[-2].endswith("-manual.db") and listing[-1].endswith("-pre-migration-from-0012.db")  # newest first


def test_the_backup_job_writes_a_daily_backup(settings: Settings, db: Database, migrated_engine: Engine) -> None:
    from hisaab.jobs.kinds import job_kinds
    result: object = job_kinds(settings)["backup"].run(JobContext(db, "job", None, {}))
    [made] = list(settings.backup_dir.iterdir())
    assert made.name.endswith("-daily.db") and result == {"file": made.name, "kept": 1}


def test_a_daily_backup_is_queued_when_the_newest_is_a_day_old(settings: Settings, db: Database,
                                                              migrated_engine: Engine) -> None:
    from hisaab.jobs.queue import JobQueue
    from hisaab.ops.backup import enqueue_due_backup

    class Recorder(JobQueue):
        def __init__(self) -> None:
            super().__init__(db, {})
            self.queued: list[str] = []

        def enqueue(self, kind: str, user_id: str | None = None, params: dict[str, object] | None = None) -> str:
            self.queued.append(kind)
            return "job"

    queue: Recorder = Recorder()
    assert enqueue_due_backup(queue, settings.backup_dir, NOW) == "job"  # none yet
    ops.backup(settings.database_path, settings.backup_dir, "daily", now=NOW - timedelta(hours=5))
    queue.queued.clear()
    assert enqueue_due_backup(queue, settings.backup_dir, NOW) is None and queue.queued == []
    assert enqueue_due_backup(queue, settings.backup_dir, NOW + timedelta(hours=20)) == "job"


# --- found in the whole-branch review ------------------------------------------------------------------------

def test_a_failing_start_retried_by_systemd_takes_one_pre_migration_backup_not_one_per_try(settings: Settings) -> None:
    migrate(settings.database_path, "0012")
    for _ in range(5):  # the migration keeps failing; each start runs this again
        assert cli.main(["backup", "--before-migrating"], settings=settings) == 0
    assert [p.name.split("-", 4)[-1] for p in ops.backups(settings.backup_dir)] == ["pre-migration-from-0012.db"]


def test_a_database_ahead_of_the_code_is_not_backed_up_over_and_over(settings: Settings) -> None:
    """Switching to an older checkout: the database's revision is unknown to this code, alembic will refuse it, and
    no backup should be taken for a migration that can't run."""
    migrate(settings.database_path)
    with sqlite3.connect(settings.database_path) as c:
        c.execute("UPDATE alembic_version SET version_num = '9999'")
    assert ops.migrations_pending(settings.database_path) is False
    assert cli.main(["backup", "--before-migrating"], settings=settings) == 0
    assert ops.backups(settings.backup_dir) == []


def test_an_interrupted_backup_never_leaves_a_file_under_a_backup_name(
        settings: Settings, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review: VACUUM INTO straight to the final name left a truncated file that then passed as the newest backup.
    A kill between writing and renaming (systemctl stop, wsl --shutdown) must leave no backup-named file."""
    def killed(*args: object) -> None:
        raise KeyboardInterrupt  # the process dies before the rename

    monkeypatch.setattr("hisaab.ops.backup.os.replace", killed)
    with pytest.raises(KeyboardInterrupt):
        ops.backup(settings.database_path, settings.backup_dir, "daily", now=NOW)
    assert ops.backups(settings.backup_dir) == [] and ops.latest(settings.backup_dir) is None


def test_a_database_path_with_hash_or_question_mark_is_backed_up_not_replaced_by_an_empty_one(
        settings: Settings, conn: Connection, tmp_path: Path) -> None:
    """Review: an unescaped file: URI cut the path at '#', and SQLite opened (and created) a different, empty file."""
    f.user(conn)
    conn.commit()
    odd: Path = tmp_path / "my db #1?.db"
    with sqlite3.connect(settings.database_path) as src, sqlite3.connect(odd) as dst:
        src.backup(dst)
    made: Path = ops.backup(odd, settings.backup_dir, "manual", now=NOW)
    assert rows(made) == 1
    assert ops.migrations_pending(odd) is False and ops.current_revision(odd) is not None


def test_a_failed_backup_prunes_nothing(settings: Settings, migrated_engine: Engine, tmp_path: Path) -> None:
    for i in range(3):
        ops.backup(settings.database_path, settings.backup_dir, "daily", now=NOW + timedelta(minutes=i))
    before: list[Path] = ops.backups(settings.backup_dir)
    with pytest.raises(ops.BackupFailed):
        ops.backup(tmp_path / "no-such.db", settings.backup_dir, "daily", now=NOW + timedelta(hours=1))
    assert ops.backups(settings.backup_dir) == before


def test_a_backup_that_cannot_prune_an_old_one_still_counts(settings: Settings, migrated_engine: Engine,
                                                            monkeypatch: pytest.MonkeyPatch,
                                                            caplog: pytest.LogCaptureFixture) -> None:
    """Review: an old backup locked on the Windows side (OneDrive) made prune raise, so a start or a re-sync was
    refused although the new backup had been written."""
    for i in range(ops.KEEP):
        ops.backup(settings.database_path, settings.backup_dir, "daily", now=NOW + timedelta(minutes=i))
    real_unlink: Callable[..., None] = Path.unlink

    def locked(self: Path, missing_ok: bool = False) -> None:
        if self.name.startswith("hisaab-"):
            raise PermissionError(13, "locked by another process", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked)
    with caplog.at_level("WARNING", logger="hisaab.ops.backup"):
        made: Path = ops.backup(settings.database_path, settings.backup_dir, "daily", now=NOW + timedelta(hours=2))
    assert made.exists() and "couldn't delete" in caplog.text
