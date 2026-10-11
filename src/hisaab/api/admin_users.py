"""/api/admin/users: admin-only user management (PRD §6.9, §10.8).

Users are created by an admin (no self-signup). Password reset is
admin-performed and ends the target's sessions. Roles can change, except that
the last admin cannot be demoted. Every route depends on require_admin, so a
member gets 403 and an anonymous caller 401 before any handler code runs.
Rule violations (users.UserError) become 409/422 in main._register_error_handlers.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from hisaab.api.deps import get_db, require_admin
from hisaab.auth import users
from hisaab.db import Database
from hisaab.models import User

router: APIRouter = APIRouter(prefix="/api/admin/users", tags=["admin"], dependencies=[Depends(require_admin)])


class ManagedUser(BaseModel):
    id: str
    username: str
    display_name: str
    role: str
    created_at: str

    @classmethod
    def of(cls, user: User) -> "ManagedUser":
        return cls(id=user.id, username=user.username, display_name=user.display_name,
                   role=user.role, created_at=user.created_at)


class NewUser(BaseModel):
    username: str
    display_name: str
    password: str
    role: str


class NewPassword(BaseModel):
    password: str


class UserChange(BaseModel):
    role: str


def _get_or_404(session: Session, user_id: str) -> User:
    user: User | None = users.get(session, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such user")
    return user


@router.get("", response_model=list[ManagedUser])
def list_users(db: Database = Depends(get_db)) -> list[ManagedUser]:
    with db.read_session() as s:
        return [ManagedUser.of(u) for u in users.list_users(s)]


@router.post("", response_model=ManagedUser, status_code=status.HTTP_201_CREATED)
def create_user(body: NewUser, request: Request, db: Database = Depends(get_db)) -> ManagedUser:
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        return ManagedUser.of(users.create_user(s, body.username, body.display_name, body.password, body.role))


@router.post("/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(user_id: str, body: NewPassword, request: Request, db: Database = Depends(get_db)) -> Response:
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        users.set_password(s, _get_or_404(s, user_id), body.password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/{user_id}", response_model=ManagedUser)
def change_user(user_id: str, body: UserChange, request: Request, db: Database = Depends(get_db)) -> ManagedUser:
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        user: User = _get_or_404(s, user_id)
        users.set_role(s, user, body.role)
        return ManagedUser.of(user)
