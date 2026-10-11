"""Request dependencies: database access, the logged-in user, the admin gate.

current_user is where sliding expiry happens on every authenticated request:
resolve the session (a read), then touch it if due (a throttled, best-effort
write) and, when touched, re-issue the cookie so the browser's expiry slides too.
"""

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Response, status

from hisaab.auth import sessions, users
from hisaab.config import Settings
from hisaab.db import Database
from hisaab.models import User, UserSession

SESSION_COOKIE: str = "hisaab_session"
COOKIE_MAX_AGE: int = int(sessions.SESSION_TTL.total_seconds())


@dataclass(frozen=True)
class CurrentUser:
    id: str
    username: str
    display_name: str
    role: str


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def session_secret(request: Request) -> str:
    settings: Settings = request.app.state.settings
    assert settings.session_secret is not None  # create_app refuses to start without one
    return settings.session_secret.get_secret_value()


def set_session_cookie(response: Response, token: str) -> None:
    # HttpOnly: unreadable from JS. SameSite=Lax: not sent on cross-site POSTs.
    # No Secure flag: plain HTTP on a trusted LAN is an accepted risk (tech-design §9.6).
    response.set_cookie(SESSION_COOKIE, token, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax", path="/")


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, httponly=True, samesite="lax", path="/")


_UNAUTHENTICATED: HTTPException = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not logged in")


def current_user(request: Request, response: Response, db: Database = Depends(get_db)) -> CurrentUser:
    token: str | None = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise _UNAUTHENTICATED
    secret: str = session_secret(request)
    found: tuple[CurrentUser, str] | None = None  # the user, and when this session was last seen
    with db.read_session() as s:  # values are copied out: ORM objects die with the session
        row: UserSession | None = sessions.resolve(s, secret, token)
        user: User | None = users.get(s, row.user_id) if row else None
        if row is not None and user is not None:
            found = (CurrentUser(user.id, user.username, user.display_name, user.role), row.last_seen_at)
    if found is None:
        sessions.purge_if_expired(db, secret, token)  # no-op unless the row exists but expired
        raise _UNAUTHENTICATED
    current, last_seen_at = found
    if sessions.touch_if_due(db, secret, token, last_seen_at):
        set_session_cookie(response, token)
    return current


def require_admin(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return user
