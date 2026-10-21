"""/api/auth: sign up, log in, log out, who am I."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from hisaab.api.deps import (
    SESSION_COOKIE,
    CurrentUser,
    clear_session_cookie,
    current_user,
    get_db,
    session_secret,
    set_session_cookie,
)
from hisaab.auth import sessions, users
from hisaab.auth.passwords import burn_verify_time, hash_password, verify_password
from hisaab.auth.throttle import LoginThrottle
from hisaab.db import Database
from hisaab.models import User

router: APIRouter = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class SignupRequest(BaseModel):
    username: str
    display_name: str
    password: str


class SignupInfo(BaseModel):
    first_user: bool  # the next account to sign up becomes the admin


class SessionUser(BaseModel):
    id: str
    username: str
    display_name: str
    role: str


_BAD_LOGIN: HTTPException = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
_LOCKED: HTTPException = HTTPException(
    status.HTTP_429_TOO_MANY_REQUESTS,
    "Too many wrong passwords for this username. Try again in 15 minutes.",
)


@router.post("/login", response_model=SessionUser)
def login(body: LoginRequest, request: Request, response: Response, db: Database = Depends(get_db)) -> SessionUser:
    throttle: LoginThrottle = request.app.state.login_throttle
    username: str = users.normalize_username(body.username)
    if throttle.is_locked(username):
        raise _LOCKED  # checked first, so a locked username costs no Argon2 time

    # 1. Read and verify outside the writer lock: Argon2 takes ~50 ms by design.
    with db.read_session() as s:
        user: User | None = users.get_by_username(s, username)
        found: tuple[str, str] | None = (user.id, user.password_hash) if user else None
    if found is None:
        burn_verify_time(body.password)  # same cost as a real check: no username probing
        throttle.record_failure(username)
        raise _BAD_LOGIN
    user_id, stored_hash = found
    ok, needs_rehash = verify_password(stored_hash, body.password)
    if not ok:
        throttle.record_failure(username)
        raise _BAD_LOGIN
    throttle.record_success(username)
    new_hash: str | None = hash_password(body.password) if needs_rehash else None

    # 2. One short write under the writer lock (WriterBusy -> 503, see main.py).
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        user = users.get(s, user_id)
        if user is None:  # deleted between the check and now
            raise _BAD_LOGIN
        if new_hash:
            user.password_hash = new_hash
        token: str = sessions.create(s, session_secret(request), user_id)
        out: SessionUser = SessionUser(
            id=user.id, username=user.username, display_name=user.display_name, role=user.role)
    set_session_cookie(response, token)
    return out


@router.get("/signup", response_model=SignupInfo)
def signup_info(db: Database = Depends(get_db)) -> SignupInfo:
    with db.read_session() as s:
        return SignupInfo(first_user=not users.list_users(s))


@router.post("/signup", response_model=SessionUser, status_code=status.HTTP_201_CREATED)
def signup(body: SignupRequest, request: Request, response: Response, db: Database = Depends(get_db)) -> SessionUser:
    """Create an account and sign it in. Rule violations become 409/422 in main._register_error_handlers, before
    any cookie is set."""
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        user: User = users.signup(s, body.username, body.display_name, body.password)
        token: str = sessions.create(s, session_secret(request), user.id)
        out: SessionUser = SessionUser(
            id=user.id, username=user.username, display_name=user.display_name, role=user.role)
    set_session_cookie(response, token)
    return out


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: Database = Depends(get_db)) -> Response:
    token: str | None = request.cookies.get(SESSION_COOKIE)
    if token:
        # Never wait for the writer (a reprocess can hold it for minutes): clearing
        # the cookie is what signs this browser out. If the row can't be deleted
        # now, it is harmless: only the browser ever had the token.
        with db.try_write_session() as s:
            if s is not None:
                sessions.delete(s, session_secret(request), token)
    clear_session_cookie(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=SessionUser)
def me(user: CurrentUser = Depends(current_user)) -> SessionUser:
    return SessionUser(id=user.id, username=user.username, display_name=user.display_name, role=user.role)
