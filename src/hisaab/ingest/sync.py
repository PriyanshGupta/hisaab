"""The sync job (tech-design §5.1, §5.6; PRD §4.4): new mail from active senders becomes Sources.

1. Window: from last_successful_sync_at less a day of overlap, but never less
   than sync_min_lookback_hours (48) back. Until a sync has fully succeeded
   once, the window is the backfill (§5.4): from local midnight on
   backfill_start, or on the earliest opening balance if older (setup checks
   anchors against the start on the day Gmail is connected; history from each
   anchor on must arrive even if the backfill begins or resumes days later). So a backfill cut short (killed, timed out, a fetch failed)
   resumes on whichever sync runs next, startup's catch-up included, and step
   3 skips what it already stored.
2. Search active senders over the window, page by page.
3. Skip message ids this user already has (idempotency, §4.10).
4. Store each new message as a pending Source: content retained for tier
   routing (slice 14), attachment metadata alongside. One write transaction per
   page, so the lock is released between pages and a crash loses one page.
5. Advance the clock to the time this sync STARTED, only if everything
   succeeded. A message that failed to fetch is counted and the sync goes on,
   but the clock stays, so the next run's window covers it again.

A sender backfill (job kind sender_backfill, params {"senders": [...]}, slice 23)
is the same job limited to those senders, from where the first backfill starts
(local midnight on backfill_start, or the earliest opening balance if older)
whatever the clock says, and it never moves the clock: an approved sender's
history arrives without changing what the regular sync covers next.

A re-sync (job kind resync, params {"from": day, "to": day}, slice 24; tech-design
§5.2) is the same job over that range of India days only, with every stored
message in it fetched again and re-parsed (reparse), never moving the clock.
derive.apply updates each transaction in place, so the precedence is per field:
an edited field keeps its edit (the overlay), a statement-primary transaction
keeps the statement's values (an email is never primary over it), and the rest
takes the new parse.

A dead grant (revoked, expired, or undecryptable with the current key) moves
the connection to needs_reauth and fails the job; the clock is untouched.
"""

import calendar
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hisaab.clock import LOCAL_TZ, local_today
from hisaab.config import Settings
from hisaab.crypto import DecryptionFailed, decrypt
from hisaab.ingest import connection
from hisaab.ingest.mailbox import Mailbox, MailboxAuthError, MailboxError, Message, Transport, default_transport
from hisaab import llm
from hisaab.jobs.queue import JobContext, JobQueue
from hisaab.models import Account, GmailConnection, Sender, Source, utc_now
from hisaab.pipeline.process import process_pending
from hisaab.transactions.repository import next_ingest_seq
from hisaab.tunables import tunable

OVERLAP: timedelta = timedelta(days=1)  # free: step 3 discards what was already stored
REAUTH: str = "Gmail access has expired or was revoked; reconnect Gmail in Settings"

_log: logging.Logger = logging.getLogger(__name__)


class SyncError(RuntimeError):
    pass


def make_sync(settings: Settings, *, transport: Transport = default_transport,
              llm_transport: llm.Transport = llm.default_transport,
              sleep: Callable[[float], None] = time.sleep) -> Callable[[JobContext], dict[str, Any]]:
    def run(ctx: JobContext) -> dict[str, Any]:
        return _sync(ctx, settings, transport, llm_transport, sleep)
    return run


def _sync(ctx: JobContext, settings: Settings, transport: Transport, llm_transport: llm.Transport,
          sleep: Callable[[float], None]) -> dict[str, Any]:
    started: datetime = datetime.now(UTC)
    user_id: str | None = ctx.user_id
    assert user_id is not None, "a sync job belongs to a user"
    with ctx.read() as s:
        row: GmailConnection | None = s.get(GmailConnection, user_id)
        if row is None or row.status != "connected":
            raise SyncError(REAUTH if row is not None else "Gmail is not connected")
        last: str | None = row.last_successful_sync_at
        blob: bytes = row.refresh_token_enc
        since: datetime = started - timedelta(hours=tunable(s, "sync_min_lookback_hours", user_id))
        senders: list[str] = list(s.scalars(select(Sender.email_address).where(Sender.status == "active")
                                            .order_by(Sender.email_address)))
        mailbox: str = row.email_address
        if last is None:  # the backfill
            since = _backfill_from(s, user_id, started)
    if last is not None:
        since = min(since, datetime.fromisoformat(last) - OVERLAP)
    only: list[str] | None = ctx.params.get("senders")  # an approved sender's backfill (slice 23)
    if only:
        with ctx.read() as s:
            since = _backfill_from(s, user_id, started)
        senders = sorted(only)
    until: datetime | None = None
    span: tuple[str, str] | None = (ctx.params["from"], ctx.params["to"]) if ctx.params.get("from") else None
    if span:  # a re-sync (slice 24): this range only, every stored message in it re-parsed
        since, until = local_midnight(span[0]), local_midnight(span[1]) + timedelta(days=1)
    reparse: bool = bool(ctx.params.get("reparse")) or span is not None
    counts: dict[str, int] = {"stored": 0, "already_had": 0, "reparsed": 0, "failed": 0, "with_attachments": 0}
    try:
        box: Mailbox = Mailbox.open(connection.oauth_client(settings),
                                    decrypt(connection.encryption_key(settings), blob), http=transport, sleep=sleep)
        for page in box.search(senders, since, until) if senders else ():
            ctx.checkpoint()
            _sync_page(ctx, user_id, box, page, counts, reparse)
    except (MailboxAuthError, DecryptionFailed) as exc:
        with ctx.write() as s:
            connection.mark_needs_reauth(s, user_id)
        raise SyncError(REAUTH) from exc
    pipeline: dict[str, int] = process_pending(ctx, user_id, settings, llm_transport, sleep)
    if counts["failed"] == 0 and not only and not span:  # pending_llm doesn't hold the clock; backfills never move it
        with ctx.write() as s:
            now_row: GmailConnection = s.get_one(GmailConnection, user_id)
            if now_row.email_address == mailbox:  # a reconnect to another mailbox reset the clock for its backfill
                now_row.last_successful_sync_at = utc_now(started)
    return {**counts, **pipeline, "since": utc_now(since)}


def _sync_page(ctx: JobContext, user_id: str, box: Mailbox, page: list[str], counts: dict[str, int],
               reparse: bool) -> None:
    with ctx.read() as s:
        have: set[str | None] = set(s.scalars(select(Source.external_message_id).where(
            Source.user_id == user_id, Source.external_message_id.in_(page))))
    fetched: list[Message] = []
    refetched: list[Message] = []
    for message_id in page:
        if message_id in have and not reparse:
            counts["already_had"] += 1
            continue
        try:
            (refetched if message_id in have else fetched).append(box.fetch(message_id))
        except MailboxAuthError:
            raise
        except MailboxError as exc:  # §5.6: logged, counted, the sync continues
            _log.warning("sync for user %s: message fetch failed: %s", user_id, exc)
            counts["failed"] += 1
    # No re-check needed: a user's syncs join into one, and the writer lock serializes all writes.
    with ctx.write() as s:
        for message in fetched:
            _store(s, user_id, message)
            counts["stored"] += 1
            counts["with_attachments"] += bool(message.attachments)
        for message in refetched:
            _requeue(s, user_id, message)
            counts["reparsed"] += 1
        ctx.report(s, {"fetched": sum(counts[k] for k in ("stored", "already_had", "reparsed", "failed")),
                       "estimate": box.estimate})


def _requeue(session: Session, user_id: str, message: Message) -> None:
    """Re-parse (the core of re-sync, §5.2): the stored Source gets its content back and waits for
    routing again. Its identity (id, ingest_seq) is unchanged, so derived keys and edits hold."""
    source: Source = session.scalars(select(Source).where(
        Source.user_id == user_id, Source.external_message_id == message.message_id)).one()
    source.content, source.processing_state = message.content, "pending"
    source.content_hash = hashlib.sha256(message.content).hexdigest()  # same bytes as before: the rebuild is stable


def _store(session: Session, user_id: str, message: Message) -> None:
    now: str = utc_now()
    session.add(Source(
        ingest_seq=next_ingest_seq(session), user_id=user_id, kind="email", content=message.content,
        content_hash=hashlib.sha256(message.content).hexdigest(), external_message_id=message.message_id,
        received_at=utc_now(message.received_at), ingested_at=now, processing_state="pending",
        attachments=json.dumps([asdict(a) for a in message.attachments]) if message.attachments else None,
    ))
    session.flush()  # so the next message's ingest_seq sees this one


def local_midnight(day: str) -> datetime:
    """The start of an India day (YYYY-MM-DD), as an aware datetime."""
    return datetime.combine(date.fromisoformat(day), day_time(), LOCAL_TZ)


def _backfill_from(session: Session, user_id: str, now: datetime) -> datetime:
    """Where a backfill starts: local midnight on backfill_start, or on the earliest opening balance if that is
    older, so history from each anchor on arrives (reconciliation needs it)."""
    anchors: list[str] = [d for d in session.scalars(select(Account.opening_balance_date).where(
        Account.user_id == user_id, Account.archived_at.is_(None))) if d is not None]
    first_day: date = min([backfill_start(session, user_id, now), *map(date.fromisoformat, anchors)])
    return datetime.combine(first_day, day_time(), LOCAL_TZ)


def backfill_start(session: Session, user_id: str, now: datetime | None = None) -> date:
    """The local date backfill_window_months calendar months before today, the day clamped to the month's end."""
    months: int = tunable(session, "backfill_window_months", user_id)
    today: date = local_today(now)
    year, month0 = divmod(today.year * 12 + today.month - 1 - months, 12)
    return date(year, month0 + 1, min(today.day, calendar.monthrange(year, month0 + 1)[1]))


def sync_kind(session: Session, user_id: str) -> str:
    """'backfill' until the user's first sync has fully succeeded, then 'sync' (only the timeout differs)."""
    row: GmailConnection | None = session.get(GmailConnection, user_id)
    return "sync" if row is not None and row.last_successful_sync_at is not None else "backfill"


def enqueue_sync(queue: JobQueue, user_id: str) -> str:
    """The one way a sync is enqueued (button, connect, schedule); joins one already queued or running."""
    with queue.db.read_session() as s:
        kind: str = sync_kind(s, user_id)
    return queue.enqueue(kind, user_id=user_id)
