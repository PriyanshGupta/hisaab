"""Re-sync a date range (tech-design §5.2, PRD §6.7): the admin's tool for applying a parser change to history.

The job is the sync job (job kind resync, params {"from", "to"}): every stored message from an active sender
received in that range of India days is fetched again from Gmail and re-parsed, per field. This module is the
admin side around it:

- affected: how many transactions a re-sync of that range could change (distinct transactions linked to an
  email received in it), for one inbox or every inbox, so the confirmation can name the number;
- enqueue_resync: one resync job per connected inbox (or the one asked for);
- resolve_notices: starting a re-sync resolves the parser-change notices whose range it covers.
"""

from datetime import timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from hisaab.ingest.sync import local_midnight
from hisaab.jobs.queue import JobQueue
from hisaab.models import GmailConnection, ResyncNotice, Source, TransactionSource, utc_now


def affected(session: Session, start: str, end: str, user_id: str | None) -> int:
    since: str = utc_now(local_midnight(start))
    until: str = utc_now(local_midnight(end) + timedelta(days=1))
    query: Select[tuple[int]] = (select(func.count(func.distinct(TransactionSource.transaction_id)))
             .join(Source, Source.id == TransactionSource.source_id)
             .where(Source.kind == "email", Source.received_at >= since, Source.received_at < until))
    if user_id is not None:
        query = query.where(Source.user_id == user_id)
    return session.scalar(query) or 0


def inboxes(session: Session, user_id: str | None) -> list[str]:
    """The connected inboxes a re-sync would cover."""
    query: Select[tuple[str]] = select(GmailConnection.user_id).where(GmailConnection.status == "connected")
    if user_id is not None:
        query = query.where(GmailConnection.user_id == user_id)
    return list(session.scalars(query))


def enqueue_resync(queue: JobQueue, start: str, end: str, user_id: str | None) -> list[str]:
    with queue.db.read_session() as s:
        users: list[str] = inboxes(s, user_id)
    return [queue.enqueue("resync", user_id=u, params={"from": start, "to": end}) for u in users]


def resolve_notices(session: Session, start: str, end: str) -> int:
    """Notices whose whole range this re-sync covers are resolved; how many."""
    rows: list[ResyncNotice] = list(session.scalars(select(ResyncNotice).where(
        ResyncNotice.resolved_at.is_(None), ResyncNotice.range_from >= start, ResyncNotice.range_to <= end)))
    for row in rows:
        row.resolved_at = utc_now()
    session.flush()
    return len(rows)
