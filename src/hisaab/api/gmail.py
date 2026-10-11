"""/api/gmail and /oauth/callback: connect a user's inbox, see its state, sync now (tech-design §5.5).

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
from typing import Literal, TypeAlias

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from hisaab.api.deps import CurrentUser, current_user, get_db, session_secret
from hisaab.api.setup import first_run_problem
from hisaab.config import Settings
from hisaab.db import Database
from hisaab.ingest import connection
from hisaab.ingest.mailbox import Grant, MailboxError, OAuthClient, authorization_url, exchange_code
from hisaab.ingest.sync import enqueue_sync, sync_kind
from hisaab.jobs.queue import JobQueue
from hisaab.models import GmailConnection

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
    return back("connected")
