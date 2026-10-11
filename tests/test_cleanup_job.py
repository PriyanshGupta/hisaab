"""The daily cleanup job (tech-design §6.7, §7): expired staging rows, old sessions, job history."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, select

from hisaab.db import Database
from hisaab.jobs import cleanup
from hisaab.jobs.queue import JobContext
from hisaab.models import Job, StatementStaging, UserSession, utc_now
from tests import factories as f


def at(delta: timedelta) -> str:
    return utc_now(datetime.now(UTC) + delta)


def run(db: Database) -> dict[str, int] | None:
    return cleanup.run(JobContext(db, "the-cleanup-job", None, {}))


def test_expired_staging_rows_go_and_live_ones_stay(db: Database, conn: Connection) -> None:
    f.statement_staging(conn, id="abandoned", expires_at=at(-timedelta(minutes=1)))
    f.statement_staging(conn, id="in-preview", expires_at=at(timedelta(hours=23)))
    conn.commit()
    assert run(db) == {"staging_rows": 1, "sessions": 0, "jobs": 0}
    with db.read_session() as s:
        assert s.scalars(select(StatementStaging.id)).all() == ["in-preview"]


def test_sessions_idle_past_30_days_go(db: Database, conn: Connection) -> None:
    f.session(conn, id="idle-31-days", last_seen_at=at(-timedelta(days=31)))
    f.session(conn, id="idle-29-days", last_seen_at=at(-timedelta(days=29)))
    conn.commit()
    assert run(db) == {"staging_rows": 0, "sessions": 1, "jobs": 0}
    with db.read_session() as s:
        assert s.scalars(select(UserSession.id)).all() == ["idle-29-days"]


def test_job_history_keeps_the_newest_100_finished_per_user(db: Database, conn: Connection) -> None:
    asha: str = f.user(conn)["id"]
    ravi: str = f.user(conn)["id"]
    start: datetime = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(103):
        f.job(conn, id=f"asha-{i:03d}", user_id=asha, state="succeeded" if i % 2 else "failed",
              created_at=utc_now(start + timedelta(hours=i)))
    for i in range(101):  # instance-wide jobs (user_id NULL) are capped as one owner
        f.job(conn, id=f"app-{i:03d}", kind="cleanup", state="succeeded", created_at=utc_now(start + timedelta(hours=i)))
    f.job(conn, id="asha-old-queued", user_id=asha, state="queued", created_at=utc_now(start - timedelta(days=1)))
    f.job(conn, id="asha-old-running", user_id=asha, state="running", created_at=utc_now(start - timedelta(days=1)))
    f.job(conn, id="ravi-0", user_id=ravi, state="cancelled", created_at=utc_now(start))
    f.job(conn, id="the-cleanup-job", kind="cleanup", state="running", created_at=utc_now(start - timedelta(days=9)))
    conn.commit()
    assert run(db) == {"staging_rows": 0, "sessions": 0, "jobs": 4}
    with db.read_session() as s:
        remaining: set[str] = set(s.scalars(select(Job.id)))
    assert {"asha-000", "asha-001", "asha-002", "app-000"}.isdisjoint(remaining)
    assert {"asha-003", "asha-102", "app-001", "app-100", "asha-old-queued", "asha-old-running", "ravi-0",
            "the-cleanup-job"} <= remaining
    assert len(remaining) == 100 + 100 + 4
