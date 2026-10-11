"""Server-side login sessions (tech-design §2.4, §9.6; PRD §10.8).

- The browser holds a random 256-bit token in an HttpOnly cookie. The database
  holds only HMAC-SHA256(session_secret, token) as sessions.id, so a copied
  .db file contains no usable session; the secret lives in .env (§9.1).
- Expiry is *sliding*: a session dies after 30 days without activity. Activity
  is recorded by touch_if_due, at most once per TOUCH_INTERVAL so that browsing
  does not turn every request into a write.
- Touching is best-effort (write-path ruling, 2026-10-10): if a job holds the
  writer lock, the touch is skipped rather than making the request wait. At
  worst a session's recorded activity lags by one interval.
"""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete as sql_delete
from sqlalchemy.orm import Session

from hisaab.db import Database
from hisaab.models import UserSession, utc_now

SESSION_TTL: timedelta = timedelta(days=30)
TOUCH_INTERVAL: timedelta = timedelta(minutes=5)
_TIMESTAMP_FORMAT: str = "%Y-%m-%dT%H:%M:%S.%fZ"


def token_id(secret: str, token: str) -> str:
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def _age(timestamp: str) -> timedelta:
    seen: datetime = datetime.strptime(timestamp, _TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    return datetime.now(UTC) - seen


def create(session: Session, secret: str, user_id: str) -> str:
    """Start a session for user_id; returns the token for the cookie (never stored)."""
    token: str = secrets.token_urlsafe(32)
    now: str = utc_now()
    session.add(UserSession(id=token_id(secret, token), user_id=user_id, created_at=now, last_seen_at=now))
    return token


def resolve(session: Session, secret: str, token: str) -> UserSession | None:
    """The live session for this token, or None if unknown or idle beyond SESSION_TTL."""
    row: UserSession | None = session.get(UserSession, token_id(secret, token))
    if row is None or _age(row.last_seen_at) > SESSION_TTL:
        return None
    return row


def purge_if_expired(db: Database, secret: str, token: str) -> None:
    """Best-effort delete of an expired session row (housekeeping; never blocks)."""
    with db.try_write_session() as session:
        if session is None:
            return
        row: UserSession | None = session.get(UserSession, token_id(secret, token))
        if row is not None and _age(row.last_seen_at) > SESSION_TTL:
            session.delete(row)


def touch_if_due(db: Database, secret: str, token: str, last_seen_at: str) -> bool:
    """Slide the expiry if the last recorded activity is older than TOUCH_INTERVAL.

    Returns True if the touch was written (the caller then re-issues the cookie
    with a fresh Max-Age, so the browser's copy slides too).
    """
    if _age(last_seen_at) < TOUCH_INTERVAL:
        return False
    with db.try_write_session() as session:
        if session is None:
            return False
        row: UserSession | None = session.get(UserSession, token_id(secret, token))
        if row is None:
            return False
        row.last_seen_at = utc_now()
        return True


def delete(session: Session, secret: str, token: str) -> None:
    session.execute(sql_delete(UserSession).where(UserSession.id == token_id(secret, token)))


def delete_all_for_user(session: Session, user_id: str) -> None:
    session.execute(sql_delete(UserSession).where(UserSession.user_id == user_id))
