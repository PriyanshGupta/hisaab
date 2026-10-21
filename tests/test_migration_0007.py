"""Migration 0007: jobs.kind gains 'backfill' (tech-design §6.7 names its 30-minute timeout).

§3.4's kind list did not include it, as it did not include 'cleanup' (0004).
"""

import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

from hisaab.config import Settings
from hisaab.jobs.kinds import job_kinds
from hisaab.jobs.queue import JobKind
from hisaab.models import utc_now
from tests.conftest import migrate

INSERT_JOB: str = "INSERT INTO jobs (id, kind, state, created_at) VALUES (:id, :kind, 'queued', :at)"
INDEX_SQL: str = "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'ix_jobs_user_state'"


def test_backfill_is_a_job_kind(conn: Connection) -> None:
    conn.execute(text(INSERT_JOB), {"id": "j1", "kind": "backfill", "at": utc_now()})


def test_unknown_kinds_are_still_rejected(conn: Connection) -> None:
    with pytest.raises(IntegrityError, match="ck_jobs_kind"):
        conn.execute(text(INSERT_JOB), {"id": "j1", "kind": "restore", "at": utc_now()})


def test_existing_jobs_and_the_index_survive(settings: Settings) -> None:
    migrate(settings.database_path, "0006")
    engine: Engine = create_engine(f"sqlite:///{settings.database_path}")
    with engine.begin() as c:
        c.execute(text(INSERT_JOB), {"id": "old", "kind": "cleanup", "at": utc_now()})
        index: str = c.execute(text(INDEX_SQL)).scalar_one()
    migrate(settings.database_path, "0007")
    with engine.begin() as c:
        assert c.execute(text("SELECT kind FROM jobs")).scalars().all() == ["cleanup"]
        assert c.execute(text(INDEX_SQL)).scalar_one() == index
        c.execute(text(INSERT_JOB), {"id": "new", "kind": "backfill", "at": utc_now()})
    engine.dispose()


def test_the_backfill_kind_runs_for_30_minutes_and_joins(settings: Settings) -> None:
    backfill: JobKind = job_kinds(settings)["backfill"]
    assert (backfill.timeout, backfill.joins) == (30 * 60, True)
