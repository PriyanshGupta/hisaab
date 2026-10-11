"""Migration 0004: jobs.kind gains 'cleanup' (ruling 2026-10-11).

tech-design §6.7 runs the daily cleanup through the job queue like every other
job, so it needs a jobs row; §3.4's kind list did not include it.
"""

import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

from hisaab.config import Settings
from hisaab.models import utc_now
from tests.conftest import migrate

INSERT_JOB: str = "INSERT INTO jobs (id, kind, state, created_at) VALUES (:id, :kind, 'queued', :at)"


def test_cleanup_is_a_job_kind(conn: Connection) -> None:
    conn.execute(text(INSERT_JOB), {"id": "j1", "kind": "cleanup", "at": utc_now()})


def test_unknown_kinds_are_still_rejected(conn: Connection) -> None:
    with pytest.raises(IntegrityError, match="ck_jobs_kind"):
        conn.execute(text(INSERT_JOB), {"id": "j1", "kind": "backup", "at": utc_now()})


def test_existing_jobs_survive(settings: Settings) -> None:
    migrate(settings.database_path, "0003")
    engine: Engine = create_engine(f"sqlite:///{settings.database_path}")
    with engine.begin() as c:
        c.execute(text(INSERT_JOB), {"id": "old", "kind": "sync", "at": utc_now()})
    migrate(settings.database_path, "0004")
    with engine.begin() as c:
        assert c.execute(text("SELECT kind FROM jobs")).scalars().all() == ["sync"]
        c.execute(text(INSERT_JOB), {"id": "new", "kind": "cleanup", "at": utc_now()})
    engine.dispose()
