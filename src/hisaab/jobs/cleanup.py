"""The daily cleanup job (tech-design §6.7), one transaction:

- statement_staging rows past expires_at (§7: an abandoned upload must not hold
  parsed financial data for more than 24 hours)
- sessions idle longer than the 30-day sliding expiry
- finished jobs beyond the newest JOB_HISTORY per user; instance-wide jobs
  (user_id NULL) are one more owner. Queued and running jobs are never touched,
  so this job cannot delete its own row.
"""

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, Delete, Subquery, delete, func, select
from sqlalchemy.orm import Session

from hisaab.auth.sessions import SESSION_TTL
from hisaab.jobs.queue import JobContext
from hisaab.models import Job, StatementStaging, UserSession, utc_now

JOB_HISTORY: int = 100
FINISHED: tuple[str, ...] = ("succeeded", "failed", "cancelled")


def _deleted(session: Session, statement: Delete) -> int:
    result: CursorResult[Any] = cast(CursorResult[Any], session.execute(
        statement.execution_options(synchronize_session=False)))  # nothing loaded to keep in sync
    return result.rowcount


def run(ctx: JobContext) -> dict[str, int]:
    now: datetime = datetime.now(UTC)
    ranked: Subquery = select(  # the window function is dialect-neutral (SQLite 3.25+, Postgres)
        Job.id, func.row_number().over(partition_by=Job.user_id, order_by=(Job.created_at.desc(), Job.id.desc()))
        .label("n"),
    ).where(Job.state.in_(FINISHED)).subquery()
    with ctx.write() as s:
        return {
            "staging_rows": _deleted(s, delete(StatementStaging).where(StatementStaging.expires_at < utc_now(now))),
            "sessions": _deleted(s, delete(UserSession).where(UserSession.last_seen_at < utc_now(now - SESSION_TTL))),
            "jobs": _deleted(s, delete(Job).where(Job.id.in_(select(ranked.c.id).where(ranked.c.n > JOB_HISTORY)))),
        }
