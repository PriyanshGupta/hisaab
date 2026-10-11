"""/api/auth: login, logout, who am I."""

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
from hisaab.db import Database

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class SessionUser(BaseModel):
    id: str
    username: str
    display_name: str
    role: str


_BAD_LOGIN = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")


@router.post("/login", response_model=SessionUser)
def login(body: LoginRequest, request: Request, response: Response, db: Database = Depends(get_db)):
    # 1. Read and verify outside the writer lock: Argon2 takes ~50 ms by design.
    with db.read_session() as s:
        user = users.get_by_username(s, body.username)
        found = (user.id, user.password_hash) if user else None
    if found is None:
        burn_verify_time(body.password)  # same cost as a real check: no username probing
        raise _BAD_LOGIN
    user_id, stored_hash = found
    ok, needs_rehash = verify_password(stored_hash, body.password)
    if not ok:
        raise _BAD_LOGIN
    new_hash = hash_password(body.password) if needs_rehash else None

    # 2. One short write under the writer lock (WriterBusy -> 503, see main.py).
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        user = users.get(s, user_id)
        if new_hash:
            user.password_hash = new_hash
        token = sessions.create(s, session_secret(request), user_id)
        out = SessionUser(id=user.id, username=user.username, display_name=user.display_name, role=user.role)
    set_session_cookie(response, token)
    return out


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: Database = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with db.write_session(timeout=request.app.state.writer_timeout) as s:
            sessions.delete(s, session_secret(request), token)
    clear_session_cookie(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=SessionUser)
def me(user: CurrentUser = Depends(current_user)):
    return SessionUser(id=user.id, username=user.username, display_name=user.display_name, role=user.role)
