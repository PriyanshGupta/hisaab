import os
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Column, Integer, MetaData, String, Table

from hisaab.config import PROJECT_ROOT
from hisaab.models import Base


def _config(db_path: Path) -> Config:
    cfg: Config = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.attributes["database_path"] = db_path
    cfg.attributes["skip_logging_config"] = True  # keep pytest's log capture intact
    return cfg


def test_upgrade_head_runs_on_empty_database_through_configured_engine(tmp_path: Path) -> None:
    db: Path = tmp_path / "m.db"
    command.upgrade(_config(db), "head")
    assert db.exists()
    # WAL is persistent in the file, so this proves env.py used make_engine.
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_autogenerate_renders_batch_operations(tmp_path: Path) -> None:
    # render_as_batch=True is what makes autogenerate emit batch_alter_table
    # blocks, which SQLite needs for anything beyond ADD COLUMN.
    db: Path = tmp_path / "m.db"
    command.upgrade(_config(db), "head")  # autogenerate refuses a database that is behind head
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY)")

    # The real schema plus one probe change, so the only diff is on `widgets`.
    target: MetaData = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(target)
    Table("widgets", target, Column("id", Integer, primary_key=True), Column("name", String(50)))

    versions: Path = tmp_path / "versions"
    versions.mkdir()
    cfg: Config = _config(db)
    cfg.attributes["target_metadata"] = target
    # alembic.ini uses path_separator = os, i.e. ":" on Linux.
    cfg.set_main_option(
        "version_locations", os.pathsep.join([str(PROJECT_ROOT / "migrations" / "versions"), str(versions)])
    )
    command.revision(cfg, message="probe", autogenerate=True, version_path=str(versions))

    (generated,) = versions.glob("*.py")
    assert "batch_alter_table('widgets'" in generated.read_text()
