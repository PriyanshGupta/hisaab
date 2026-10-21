"""/api/gmail and /oauth/callback: connect a user's inbox, see its state, sync now, sync a date range (tech-design
§5.5).

Connect: POST /api/gmail/connect returns Google's consent URL carrying a signed
`state` (user id, expiry, nonce, HMAC with the session secret). Google sends
the browser back to http://localhost:<port>/oauth/callback, the loopback
redirect a Desktop OAuth client allows. The callback trusts the state, not a
cookie: someone who opened the app at a LAN address has no session cookie for
localhost. The code exchange is network I/O, done before the short write that
stores the grant. A connect is followed by a sync: the backfill, the first
time. During first run, connect waits for accounts and their opening balances
(tech-design §5.4) and the callback returns to /setup.
"""

import hashlib
import hmac
import secrets
import time
from datetime import date, timedelta
from typing import Literal, TypeAlias

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select

from hisaab.api.deps import CurrentUser, current_user, get_db, session_secret
from hisaab.api.setup import first_run_problem
from hisaab.clock import local_today
from hisaab.config import Settings
from hisaab.db import Database
from hisaab.ingest import connection
from hisaab.ingest.resync import affected, enqueue_resync
from hisaab.ingest.mailbox import Grant, MailboxError, OAuthClient, authorization_url, exchange_code
from hisaab.ingest.sync import enqueue_sync, sync_kind
from hisaab.senders.review import enqueue_missed_backfills
from hisaab.jobs.queue import JobQueue
from hisaab.models import GmailConnection, Job
from hisaab.ops.backup import BackupFailed, backup

router: APIRouter = APIRouter(prefix="/api/gmail", tags=["gmail"])
callback_router: APIRouter = APIRouter()

STATE_TTL_S: int = 600

ConnectionStatus: TypeAlias = Literal["not_connected", "connected", "needs_reauth", "disconnected"]


class GmailStatus(BaseModel):
    configured: bool  # the OAuth client and encryption key are in .env
    status: ConnectionStatus
    email_address: str | None
    last_successful_sync_at: str | None


class ConnectOut(BaseModel):
    authorization_url: str


class JobRef(BaseModel):
    job_id: str


class DateRange(BaseModel):
    start: str  # India dates, YYYY-MM-DD, both included
    end: str


class RangePreview(BaseModel):
    transactions: int  # of yours, from emails received in the range: they are read again


MAX_RANGE_DAYS: int = 184  # about six months: a longer sync risks the resync job's 30-minute limit


def _mac(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), f"oauth-state:{payload}".encode(), hashlib.sha256).hexdigest()


def sign_state(secret: str, user_id: str, ttl_s: int = STATE_TTL_S) -> str:
    payload: str = f"{user_id}.{int(time.time()) + ttl_s}.{secrets.token_urlsafe(8)}"
    return f"{payload}.{_mac(secret, payload)}"


def verify_state(secret: str, state: str) -> str | None:
    """The user id the state was issued to, or None if forged, altered or expired."""
    payload, _, mac = state.rpartition(".")
    parts: list[str] = payload.split(".")
    if len(parts) != 3 or not hmac.compare_digest(mac, _mac(secret, payload)):
        return None
    user_id, expires, _nonce = parts
    return user_id if expires.isdigit() and int(expires) >= time.time() else None


def _configured(settings: Settings) -> bool:
    try:
        connection.oauth_client(settings)
        connection.encryption_key(settings)
    except connection.GmailNotConfigured:
        return False
    return True


@router.get("", response_model=GmailStatus)
def gmail_status(request: Request, user: CurrentUser = Depends(current_user),
                 db: Database = Depends(get_db)) -> GmailStatus:
    with db.read_session() as s:
        row: GmailConnection | None = s.get(GmailConnection, user.id)
        configured: bool = _configured(request.app.state.settings)
        if row is None:
            return GmailStatus(configured=configured, status="not_connected", email_address=None,
                               last_successful_sync_at=None)
        return GmailStatus(configured=configured, status=row.status,  # pydantic checks the Literal
                           email_address=row.email_address, last_successful_sync_at=row.last_successful_sync_at)


@router.post("/connect", response_model=ConnectOut)
def connect(request: Request, user: CurrentUser = Depends(current_user),
            db: Database = Depends(get_db)) -> ConnectOut:
    settings: Settings = request.app.state.settings
    client: OAuthClient = connection.oauth_client(settings)
    connection.encryption_key(settings)  # refuse now rather than after the user has consented
    with db.read_session() as s:
        problem: str | None = first_run_problem(s, user.id)  # §5.4: accounts and balances before Gmail
    if problem is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, problem)
    return ConnectOut(authorization_url=authorization_url(client, sign_state(session_secret(request), user.id)))


@router.post("/sync", response_model=JobRef, status_code=status.HTTP_202_ACCEPTED)
def sync_now(request: Request, user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)) -> JobRef:
    with db.read_session() as s:
        row: GmailConnection | None = s.get(GmailConnection, user.id)
        state: str | None = row.status if row else None
    if state != "connected":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Connect Gmail first." if state is None else "Reconnect Gmail first.")
    jobs: JobQueue = request.app.state.jobs
    return JobRef(job_id=enqueue_sync(jobs, user.id))  # joins a queued or running sync or backfill


def _own_range(start: str, end: str) -> None:
    """A range the caller may sync: real dates, in order, not past today, at most about six months."""
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Dates must look like 2026-10-05.") from None
    if last < first:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The range ends before it starts.")
    if last > local_today():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The range can't reach into the future.")
    if last - first > timedelta(days=MAX_RANGE_DAYS):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "Pick up to 6 months at a time; sync a longer stretch in parts.")


@router.get("/resync/preview", response_model=RangePreview)
def resync_preview(start: str = "", end: str = "", user: CurrentUser = Depends(current_user),
                   db: Database = Depends(get_db)) -> RangePreview:
    _own_range(start, end)
    with db.read_session() as s:
        return RangePreview(transactions=affected(s, start, end, user.id))


@router.post("/resync", response_model=JobRef, status_code=status.HTTP_202_ACCEPTED)
def resync(body: DateRange, request: Request, user: CurrentUser = Depends(current_user),
           db: Database = Depends(get_db)) -> JobRef:
    """Sync a date range of the caller's own inbox (user, 2026-10-21: Settings, for everyone; it was an admin
    tool): new emails in it are fetched, ones already here read again with today's templates, edits kept. The
    inbox is always the caller's: nothing in the request can name another. One at a time per person (a resync
    never joins), and a backup first (tech-design §9.2): without one, nothing runs."""
    _own_range(body.start, body.end)
    with db.read_session() as s:
        row: GmailConnection | None = s.get(GmailConnection, user.id)
        state: str | None = row.status if row else None
        running: bool = s.scalars(select(Job.id).where(
            Job.kind == "resync", Job.user_id == user.id, Job.state.in_(("queued", "running")))).first() is not None
    if state != "connected":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Connect Gmail first." if state is None else "Reconnect Gmail first.")
    if running:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "A date-range sync is already running for your inbox. Try again when it finishes.")
    settings: Settings = request.app.state.settings
    try:
        backup(settings.database_path, settings.backup_dir, "pre-resync")
    except BackupFailed as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"Couldn't take the backup first, so nothing was synced: {e}.") from None
    [job_id] = enqueue_resync(request.app.state.jobs, body.start, body.end, user.id)
    return JobRef(job_id=job_id)


@callback_router.get(connection.CALLBACK_PATH, include_in_schema=False)
def oauth_callback(request: Request, state: str = "", code: str = "", error: str = "") -> RedirectResponse:
    user_id: str | None = verify_state(session_secret(request), state)
    db: Database = request.app.state.db
    page: str = "/settings"
    if user_id is not None:
        with db.read_session() as s:
            if sync_kind(s, user_id) == "backfill":
                page = "/setup"  # first run: back to the setup steps

    def back(outcome: str) -> RedirectResponse:
        return RedirectResponse(f"{page}?gmail={outcome}", status_code=status.HTTP_303_SEE_OTHER)

    if error:  # e.g. access_denied: the user declined on Google's screen
        return back("denied")
    if user_id is None or not code:
        return back("error")
    settings: Settings = request.app.state.settings
    try:
        grant: Grant = exchange_code(connection.oauth_client(settings), code, http=request.app.state.mail_transport)
        key: str = connection.encryption_key(settings)
    except (MailboxError, connection.GmailNotConfigured):
        return back("error")
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        connection.save_grant(s, user_id, grant, key)
    enqueue_sync(request.app.state.jobs, user_id)  # the backfill, on a first connection
    enqueue_missed_backfills(request.app.state.jobs, user_id)  # senders approved while the grant was dead
    return back("connected")
