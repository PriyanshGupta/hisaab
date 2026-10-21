"""Approving or ignoring a sender the discovery sweep found (PRD §6.10, tech-design §5.3).

The registry is GLOBAL: approving a sender activates it for every inbox's sync, which is why the queue shows
whose inbox it was found in. Approval also backfills that sender's mail over the backfill window for every
connected inbox (a sender_backfill job each), so a bank discovered in month four still brings its history; an
inbox waiting to be reconnected gets its backfill when it reconnects (enqueue_missed_backfills).
Ignoring keeps the sender out of every future sweep.

Either way the candidates' previews are discarded (discarded_at set, examples emptied): the sweep's findings
are held, not kept (PRD §4.3), and a newsletter's subjects have no reason to stay.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from hisaab.jobs.queue import JobQueue
from hisaab.models import GmailConnection, Sender, SenderCandidate, utc_now


class SenderNotFound(LookupError):
    pass


class NotACandidate(ValueError):
    pass


def approve(session: Session, sender_id: str) -> str:
    """The sender's address; its mail is fetched by every sync from now on."""
    sender: Sender = _candidate(session, sender_id)
    sender.status, sender.reviewed_at = "active", utc_now()
    _discard(session, sender.email_address)
    return sender.email_address


def ignore(session: Session, sender_id: str) -> None:
    sender: Sender = _candidate(session, sender_id)
    sender.status, sender.reviewed_at = "ignored", utc_now()
    _discard(session, sender.email_address)


def enqueue_backfills(queue: JobQueue, address: str) -> list[str]:
    """Three months (backfill_window_months) of the approved sender's mail, for every connected inbox."""
    with queue.db.read_session() as s:
        users: list[str] = list(s.scalars(select(GmailConnection.user_id).where(GmailConnection.status == "connected")))
    return [queue.enqueue("sender_backfill", user_id=u, params={"senders": [address]}) for u in users]


def enqueue_missed_backfills(queue: JobQueue, user_id: str) -> str | None:
    """On a reconnect: one backfill of the senders approved since this inbox last synced. Approval only queued
    backfills for inboxes connected at the time, and a reconnected inbox's sync resumes from its last sync, so
    without this their history would never arrive. A first connection (no clock yet) backfills every active
    sender anyway. A sender approved while this inbox was still syncing is fetched twice at worst: the sync
    skips messages it already has."""
    with queue.db.read_session() as s:
        row: GmailConnection | None = s.get(GmailConnection, user_id)
        last: str | None = row.last_successful_sync_at if row is not None else None
        missed: list[str] = [] if last is None else list(s.scalars(select(Sender.email_address).where(
            Sender.status == "active", Sender.reviewed_at > last).order_by(Sender.email_address)))
    return queue.enqueue("sender_backfill", user_id=user_id, params={"senders": missed}) if missed else None


def _candidate(session: Session, sender_id: str) -> Sender:
    sender: Sender | None = session.get(Sender, sender_id)
    if sender is None:
        raise SenderNotFound("No such sender.")
    if sender.status != "candidate":
        raise NotACandidate(f"{sender.email_address} has already been reviewed ({sender.status}).")
    return sender


def _discard(session: Session, address: str) -> None:
    stamp: str = utc_now()
    for row in session.scalars(select(SenderCandidate).where(SenderCandidate.sender_address == address,
                                                              SenderCandidate.discarded_at.is_(None))):
        row.discarded_at, row.matched_subjects = stamp, "[]"
    session.flush()
