from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, text

from hisaab.config import Settings
from hisaab.db import UNSAFE_FILESYSTEMS, UnsafeDatabaseLocation, assert_safe_db_location, filesystem_type, make_engine

# Shaped like WSL2's /proc/mounts: ext4 root, Windows C: drive over 9p.
WSL_MOUNTS: str = (
    "/dev/sdc / ext4 rw,relatime 0 0\n"
    "none /tmp tmpfs rw 0 0\n"
    "C:\\134 /mnt/c 9p rw,noatime,aname=drvfs;path=C:\\ 0 0\n"
    "/dev/sdd /mnt/my\\040disk ext4 rw 0 0\n"
)

# synchronous: 1 == NORMAL
EXPECTED: dict[str, Any] = {"journal_mode": "wal", "busy_timeout": 5000, "synchronous": 1, "foreign_keys": 1}


def _pragmas(conn: Connection) -> dict[str, object]:
    return {
        name: conn.execute(text(f"PRAGMA {name}")).scalar()
        for name in ("journal_mode", "busy_timeout", "synchronous", "foreign_keys")
    }


def test_fresh_connection_has_all_four_pragmas(settings: Settings) -> None:
    engine: Engine = make_engine(settings.database_path)
    with engine.connect() as conn:
        assert _pragmas(conn) == EXPECTED
    engine.dispose()


def test_pragmas_apply_to_every_pooled_connection_not_just_the_first(settings: Settings) -> None:
    engine: Engine = make_engine(settings.database_path)
    with engine.connect() as a, engine.connect() as b:  # two live DBAPI connections
        assert a.connection.dbapi_connection is not b.connection.dbapi_connection
        assert _pragmas(a) == EXPECTED
        assert _pragmas(b) == EXPECTED
    engine.dispose()


def test_foreign_keys_can_be_disabled_for_migrations_only(settings: Settings) -> None:
    engine: Engine = make_engine(settings.database_path, enforce_foreign_keys=False)
    with engine.connect() as conn:
        assert _pragmas(conn) == {**EXPECTED, "foreign_keys": 0}
    engine.dispose()


def test_creates_missing_parent_directory(tmp_path: Path) -> None:
    db: Path = tmp_path / "not" / "yet" / "there.db"
    engine: Engine = make_engine(db)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    engine.dispose()
    assert db.exists()


def test_filesystem_type_uses_longest_matching_mount_point() -> None:
    assert filesystem_type(Path("/home/u/app/data/x.db"), WSL_MOUNTS) == "ext4"
    assert filesystem_type(Path("/mnt/c/Users/u/x.db"), WSL_MOUNTS) == "9p"
    assert filesystem_type(Path("/mnt/my disk/x.db"), WSL_MOUNTS) == "ext4"  # \040 decoded
    assert filesystem_type(Path("/mnt/cdrom/x.db"), WSL_MOUNTS) == "ext4"  # /mnt/c is not a prefix of /mnt/cdrom


def test_accepts_linux_filesystem() -> None:
    assert_safe_db_location(Path("/home/u/app/data/x.db"), WSL_MOUNTS)


def test_rejects_windows_mount() -> None:
    with pytest.raises(UnsafeDatabaseLocation, match="/mnt/c"):
        assert_safe_db_location(Path("/mnt/c/Users/u/x.db"), WSL_MOUNTS)


def test_rejects_symlink_that_points_into_windows_mount(tmp_path: Path) -> None:
    link: Path = tmp_path / "data"
    link.symlink_to("/mnt/c/Users/u/hisaab-data")  # target need not exist
    with pytest.raises(UnsafeDatabaseLocation):
        assert_safe_db_location(link / "x.db", WSL_MOUNTS)


def test_rejects_drive_letter_path_even_if_mount_table_disagrees() -> None:
    with pytest.raises(UnsafeDatabaseLocation):
        assert_safe_db_location(Path("/mnt/d/x.db"), "/dev/sdc / ext4 rw 0 0\n")


def test_make_engine_refuses_unsafe_location() -> None:
    with pytest.raises(UnsafeDatabaseLocation):
        make_engine(Path("/mnt/c/hisaab-should-never-exist/x.db"))
    assert not Path("/mnt/c/hisaab-should-never-exist").exists()


def test_default_database_location_is_on_a_safe_linux_filesystem() -> None:
    path: Path = Settings(_env_file=None).database_path
    assert filesystem_type(path.resolve()) not in UNSAFE_FILESYSTEMS
    assert_safe_db_location(path)
