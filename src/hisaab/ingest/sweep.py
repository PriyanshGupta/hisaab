"""The discovery sweep (tech-design §5.3, PRD §4.3 track 2, §6.10): one job per connected inbox.

Financial language from senders the registry doesn't know becomes candidates for the admin to review. A
candidate holds the sender's address, matching subjects and Gmail's short previews ONLY, never bodies: the
mailbox's peek reads format=metadata and nothing else.

    query    mailbox.DISCOVERY_TERMS, excluding active and ignored senders and the user's own address,
             from local midnight on sync.backfill_start: the sweep lookback IS the backfill window (one
             setting, backfill_window_months), so a sender is discoverable exactly as far back as its
             history can still be backfilled
    rounds   read one page, then search again excluding every sender seen: a busy sender's hundreds of
             alerts cost one page, not all of them. A page that taught nothing to exclude (only mail with
             no usable address, say) is followed by the next page of the same search instead. Stops when
             the search runs out, after MAX_ROUNDS pages or MAX_PEEKS emails, or once CANDIDATE_CAP new
             senders are found. A message is read once per sweep, however often a search returns it
    store    a sender not yet known: a global senders row, status 'candidate'. Per inbox: a
             sender_candidates row with the EXAMPLES newest {subject, snippet, received_at} and how many
             matching emails the sweep read. A candidate still waiting is refreshed (examples, count) and
             keeps first_seen_at. Senders past the cap are not stored: the next sweep finds them

Active and ignored senders are excluded in the query AND filtered here, so an ignored newsletter never
returns even if Gmail's search lets it through; so are every household inbox's own address and any From with
no usable address. A message that can't be read is counted and skipped (§5.6). Gmail is read outside the writer lock; one write at the end.

enqueue_due_sweeps is the schedule: every connected inbox whose last sweep is older than
discovery_sweep_interval_days (or which was never swept), checked at startup and every few hours, so a
laptop that sleeps still sweeps weekly. enqueue_sweeps is "Run sweep now".
"""

import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from datetime import time as day_time
from typing import Any

from sqlalchemy import func, select

from hisaab.clock import LOCAL_TZ
from hisaab.config import Settings
from hisaab.crypto import DecryptionFailed, decrypt
from hisaab.ingest import connection
from hisaab.ingest.mailbox import (
    DISCOVERY_TERMS,
    Mailbox,
    MailboxAuthError,
    MailboxError,
    Peek,
    Transport,
    default_transport,
)
from hisaab.ingest.sync import REAUTH, SyncError, backfill_start
from hisaab.jobs.queue import JobContext, JobQueue
from hisaab.models import GmailConnection, Job, Sender, SenderCandidate, utc_now
from hisaab.tunables import tunable

CANDIDATE_CAP: int = 20  # new candidates per sweep (§5.3)
MAX_PEEKS: int = 400  # emails read per sweep, at most
MAX_ROUNDS: int = 10  # searches per sweep, at most
EXAMPLES: int = 5  # previews kept per candidate
_ADDRESS: re.Pattern[str] = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")  # a sender worth reviewing has a real address


def make_sweep(settings: Settings, *, transport: Transport = default_transport,
               sleep: Callable[[float], None] = time.sleep) -> Callable[[JobContext], dict[str, Any]]:
    def run(ctx: JobContext) -> dict[str, Any]:
        return _sweep(ctx, settings, transport, sleep)
    return run


def _sweep(ctx: JobContext, settings: Settings, transport: Transport,
           sleep: Callable[[float], None]) -> dict[str, Any]:
    user_id: str | None = ctx.user_id
    assert user_id is not None, "a sweep belongs to an inbox"
    with ctx.read() as s:
        row: GmailConnection | None = s.get(GmailConnection, user_id)
        if row is None or row.status != "connected":
            raise SyncError(REAUTH if row is not None else "Gmail is not connected")
        blob: bytes = row.refresh_token_enc
        own: str = row.email_address.strip().lower()
        # active senders first, then ignored ones newest first: the order the query keeps them in when it is full
        registered: list[str] = list(s.scalars(select(Sender.email_address).where(
            Sender.status.in_(("active", "ignored"))).order_by(
            (Sender.status == "ignored"), Sender.reviewed_at.desc().nulls_last(), Sender.email_address)))
        waiting: set[str] = set(s.scalars(select(Sender.email_address).where(Sender.status == "candidate")))
        # every household inbox: a family member's own mail is never a sender to review
        household: set[str] = {a.strip().lower() for a in s.scalars(select(GmailConnection.email_address))}
        since: datetime = datetime.combine(backfill_start(s, user_id), day_time(), LOCAL_TZ)
    try:
        box: Mailbox = Mailbox.open(connection.oauth_client(settings),
                                    decrypt(connection.encryption_key(settings), blob), http=transport, sleep=sleep)
        found, examined, unreadable = _rounds(ctx, box, registered, household | {own}, waiting, since)
    except (MailboxAuthError, DecryptionFailed) as exc:
        with ctx.write() as s:
            connection.mark_needs_reauth(s, user_id)
        raise SyncError(REAUTH) from exc
    new: int = 0
    refreshed: int = 0
    with ctx.write() as s:
        statuses: dict[str, str] = dict(s.execute(select(Sender.email_address, Sender.status).where(
            Sender.email_address.in_(found))).tuples().all())
        for address, peeks in found.items():
            if statuses.get(address) in ("active", "ignored"):  # reviewed while the sweep ran
                continue
            if address not in statuses:
                s.add(Sender(email_address=address, status="candidate"))
            candidate: SenderCandidate | None = s.scalars(select(SenderCandidate).where(
                SenderCandidate.discovered_by_user_id == user_id, SenderCandidate.sender_address == address)).first()
            examples: str = json.dumps([{"subject": p.subject, "snippet": p.snippet,
                                         "received_at": utc_now(p.received_at)} for p in peeks[:EXAMPLES]],
                                       ensure_ascii=False)
            if candidate is None:
                s.add(SenderCandidate(discovered_by_user_id=user_id, sender_address=address,
                                      matched_subjects=examples, match_count=len(peeks), first_seen_at=utc_now()))
            else:
                candidate.matched_subjects, candidate.match_count, candidate.discarded_at = examples, len(peeks), None
            new += address not in waiting
            refreshed += address in waiting
    return {"examined": examined, "unreadable": unreadable, "new_candidates": new, "refreshed": refreshed,
            "since": utc_now(since)}


def _rounds(ctx: JobContext, box: Mailbox, registered: list[str], household: set[str], waiting: set[str],
            since: datetime) -> tuple[dict[str, list[Peek]], int, int]:
    """Matching emails grouped by sender, newest first; how many emails were read; how many could not be.
    The query leaves the user's own mail out with -from:me; the filter below repeats every exclusion and adds
    the household's inboxes. A From with no usable address is skipped and never written into a query."""
    excluded: set[str] = set(registered) | household
    found: dict[str, list[Peek]] = {}
    skipped: set[str] = set()  # new senders past the cap, and excluded senders Gmail let through
    examined: int = 0
    unreadable: int = 0
    new: int = 0
    read: set[str] = set()  # message ids already looked at: a search run again returns them again
    token: str | None = None
    for _ in range(MAX_ROUNDS):
        ctx.checkpoint()
        # what this sweep has already seen matters most to leave out; the registry after it
        ids, next_token = box.discover(DISCOVERY_TERMS, [*found, *sorted(skipped), *registered], since, token)
        learned: bool = False  # a sender to leave out of the next search
        for message_id in ids:
            if examined + unreadable >= MAX_PEEKS:
                break
            if message_id in read:
                continue
            read.add(message_id)
            try:
                peek: Peek = box.peek(message_id)
            except MailboxAuthError:
                raise
            except MailboxError:  # §5.6: one message that can't be read is counted, the sweep goes on
                unreadable += 1
                continue
            examined += 1
            sender: str = peek.sender
            if not _ADDRESS.fullmatch(sender):  # "Bank Alerts", "undisclosed-recipients:;": never in a query
                continue
            if sender in excluded or sender in skipped:
                learned = learned or sender not in skipped
                skipped.add(sender)
                continue
            if sender not in found:
                learned = True
                if sender not in waiting and new >= CANDIDATE_CAP:
                    skipped.add(sender)
                    continue
                found[sender] = []
                new += sender not in waiting
            found[sender].append(peek)
        if not ids or examined + unreadable >= MAX_PEEKS or new >= CANDIDATE_CAP:
            break
        if learned:
            token = None  # search again, leaving out every sender seen so far
        elif next_token:
            token = next_token  # nothing new to leave out: read on in the same search
        else:
            break
    for peeks in found.values():
        peeks.sort(key=lambda p: p.received_at, reverse=True)
    return found, examined, unreadable


def enqueue_sweeps(queue: JobQueue) -> list[str]:
    """Run sweep now: one sweep per connected inbox (a needs_reauth grant would only fail)."""
    with queue.db.read_session() as s:
        users: list[str] = list(s.scalars(select(GmailConnection.user_id).where(GmailConnection.status == "connected")))
    return [queue.enqueue("sweep", user_id=u) for u in users]


def enqueue_due_sweeps(queue: JobQueue, now: datetime | None = None) -> list[str]:
    """The weekly schedule: a sweep for every connected inbox not swept within the interval."""
    now = now or datetime.now(UTC)
    with queue.db.read_session() as s:
        days: int = tunable(s, "discovery_sweep_interval_days")
        last: dict[str | None, str] = dict(s.execute(select(Job.user_id, func.max(Job.created_at)).where(
            Job.kind == "sweep", Job.state.in_(("queued", "running", "succeeded"))).group_by(Job.user_id)).tuples().all())
        users: list[str] = list(s.scalars(select(GmailConnection.user_id).where(GmailConnection.status == "connected")))
    floor: str = utc_now(now - timedelta(days=days))
    return [queue.enqueue("sweep", user_id=u) for u in users if last.get(u) is None or last[u] < floor]
