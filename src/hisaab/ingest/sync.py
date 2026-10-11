"""The sync job (tech-design §5.1, §5.6; PRD §4.4): new mail from active senders becomes Sources.

1. Window: from last_successful_sync_at less a day of overlap, but never less
   than sync_min_lookback_hours (48) back. A first sync is that minimum;
   the three-month backfill is slice 16.
2. Search active senders over the window, page by page.
3. Skip message ids this user already has (idempotency, §4.10).
4. Store each new message as a pending Source: content retained for tier
   routing (slice 14), attachment metadata alongside. One write transaction per
   page, so the lock is released between pages and a crash loses one page.
5. Advance the clock to the time this sync STARTED, only if everything
   succeeded. A message that failed to fetch is counted and the sync goes on,
   but the clock stays, so the next run's window covers it again.

A dead grant (revoked, expired, or undecryptable with the current key) moves
the connection to needs_reauth and fails the job; the clock is untouched.
"""

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hisaab.config import Settings
from hisaab.crypto import DecryptionFailed, decrypt
from hisaab.ingest import connection
from hisaab.ingest.mailbox import Transport, Mailbox, MailboxAuthError, MailboxError, Message, default_transport
from hisaab.jobs.queue import JobContext
from hisaab.models import GmailConnection, Sender, Source, utc_now
from hisaab.transactions.repository import next_ingest_seq
from hisaab.tunables import tunable

OVERLAP: timedelta = timedelta(days=1)  # free: step 3 discards what was already stored
REAUTH: str = "Gmail access has expired or was revoked; reconnect Gmail in Settings"

_log: logging.Logger = logging.getLogger(__name__)


class SyncError(RuntimeError):
    pass


def make_sync(settings: Settings, *, http: Transport = default_transport,
              sleep: Callable[[float], None] = time.sleep) -> Callable[[JobContext], dict[str, Any]]:
    def run(ctx: JobContext) -> dict[str, Any]:
        return _sync(ctx, settings, http, sleep)
    return run


def _sync(ctx: JobContext, settings: Settings, http: Transport, sleep: Callable[[float], None]) -> dict[str, Any]:
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
    if last is not None:
        since = min(since, datetime.fromisoformat(last) - OVERLAP)
    counts: dict[str, int] = {"stored": 0, "already_had": 0, "failed": 0, "with_attachments": 0}
    try:
        box: Mailbox = Mailbox.open(connection.oauth_client(settings),
                                    decrypt(connection.encryption_key(settings), blob), http=http, sleep=sleep)
        for page in box.search(senders, since) if senders else ():
            ctx.checkpoint()
            _sync_page(ctx, user_id, box, page, counts)
    except (MailboxAuthError, DecryptionFailed) as exc:
        with ctx.write() as s:
            connection.mark_needs_reauth(s, user_id)
        raise SyncError(REAUTH) from exc
    if counts["failed"] == 0:
        with ctx.write() as s:
            s.get_one(GmailConnection, user_id).last_successful_sync_at = utc_now(started)
    return {**counts, "since": utc_now(since)}


def _sync_page(ctx: JobContext, user_id: str, box: Mailbox, page: list[str], counts: dict[str, int]) -> None:
    with ctx.read() as s:
        have: set[str | None] = set(s.scalars(select(Source.external_message_id).where(
            Source.user_id == user_id, Source.external_message_id.in_(page))))
    fetched: list[Message] = []
    for message_id in page:
        if message_id in have:
            counts["already_had"] += 1
            continue
        try:
            fetched.append(box.fetch(message_id))
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


def _store(session: Session, user_id: str, message: Message) -> None:
    now: str = utc_now()
    session.add(Source(
        ingest_seq=next_ingest_seq(session), user_id=user_id, kind="email", content=message.content,
        content_hash=hashlib.sha256(message.content).hexdigest(), external_message_id=message.message_id,
        received_at=utc_now(message.received_at), ingested_at=now, processing_state="pending",
        attachments=json.dumps([asdict(a) for a in message.attachments]) if message.attachments else None,
    ))
    session.flush()  # so the next message's ingest_seq sees this one
