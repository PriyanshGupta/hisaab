"""Migration 0011: jobs.kind gains 'sender_backfill' (slice 23: an approved sender's three months of mail).

It is its own kind because jobs of one kind join per user: a queued regular sync would swallow it.
"""

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

from hisaab.config import Settings
from hisaab.jobs.kinds import job_kinds
from hisaab.jobs.queue import JobKind
from hisaab.models import utc_now
from tests.conftest import alembic_config, migrate

INSERT_JOB: str = "INSERT INTO jobs (id, kind, state, created_at) VALUES (:id, :kind, 'queued', :at)"
INDEX_SQL: str = "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'ix_jobs_user_state'"


def test_sender_backfill_is_a_job_kind(conn: Connection) -> None:
    conn.execute(text(INSERT_JOB), {"id": "j1", "kind": "sender_backfill", "at": utc_now()})


def test_unknown_kinds_are_still_rejected(conn: Connection) -> None:
    with pytest.raises(IntegrityError, match="ck_jobs_kind"):
        conn.execute(text(INSERT_JOB), {"id": "j1", "kind": "backup", "at": utc_now()})


def test_existing_jobs_and_the_index_survive_and_downgrade_removes_the_kind(settings: Settings) -> None:
    migrate(settings.database_path, "0010")
    engine: Engine = create_engine(f"sqlite:///{settings.database_path}")
    with engine.begin() as c:
        c.execute(text(INSERT_JOB), {"id": "old", "kind": "backfill", "at": utc_now()})
        index: str = c.execute(text(INDEX_SQL)).scalar_one()
    migrate(settings.database_path, "0011")
    with engine.begin() as c:
        assert c.execute(text("SELECT kind FROM jobs")).scalars().all() == ["backfill"]
        assert c.execute(text(INDEX_SQL)).scalar_one() == index
        c.execute(text(INSERT_JOB), {"id": "new", "kind": "sender_backfill", "at": utc_now()})
    command.downgrade(alembic_config(settings.database_path), "0010")
    with engine.begin() as c:
        assert c.execute(text("SELECT kind FROM jobs")).scalars().all() == ["backfill"]  # sender_backfill rows go
    engine.dispose()


def test_the_sender_backfill_runs_for_30_minutes_and_never_joins(settings: Settings) -> None:
    kind: JobKind = job_kinds(settings)["sender_backfill"]
    assert (kind.timeout, kind.joins) == (30 * 60, False)
