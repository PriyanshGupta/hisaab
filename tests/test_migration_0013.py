"""Migration 0013: jobs.kind gains 'backup' (tech-design §6.7: backups through the same queue as every job)."""

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

from hisaab.config import Settings
from hisaab.models import utc_now
from tests.conftest import alembic_config, migrate

INSERT_JOB: str = "INSERT INTO jobs (id, kind, state, created_at) VALUES (:id, :kind, 'queued', :at)"


def test_backup_is_a_job_kind(conn: Connection) -> None:
    conn.execute(text(INSERT_JOB), {"id": "j1", "kind": "backup", "at": utc_now()})


def test_unknown_kinds_are_still_rejected(conn: Connection) -> None:
    with pytest.raises(IntegrityError, match="ck_jobs_kind"):
        conn.execute(text(INSERT_JOB), {"id": "j1", "kind": "restore", "at": utc_now()})


def test_upgrade_and_downgrade(settings: Settings) -> None:
    migrate(settings.database_path, "0012")
    engine: Engine = create_engine(f"sqlite:///{settings.database_path}")
    migrate(settings.database_path, "0013")
    with engine.begin() as c:
        c.execute(text(INSERT_JOB), {"id": "b", "kind": "backup", "at": utc_now()})
    command.downgrade(alembic_config(settings.database_path), "0012")
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM jobs")).scalar() == 0
    engine.dispose()
