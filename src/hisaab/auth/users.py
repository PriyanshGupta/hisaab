"""Users repository: the only code that writes the users table.

Rules owned here:
- Usernames are case-insensitive: stored lowercased and trimmed, and limited
  to [a-z0-9._-]{3,32}, so "Asha" and "asha " are the same login.
- Passwords are at least 8 characters, stored only as Argon2 hashes.
- Roles are a stored attribute (admin | member), never "whoever came first"
  (PRD §8.6). The last admin cannot be demoted, so the instance can't lock
  itself out of administration. `python -m hisaab create-admin` is the
  out-of-band recovery path.
"""

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hisaab.auth import sessions
from hisaab.auth.passwords import hash_password
from hisaab.categories import repository as categories
from hisaab.models import User
from hisaab.models.core import USER_ROLES

USERNAME_PATTERN: re.Pattern[str] = re.compile(r"[a-z0-9._-]{3,32}")
MIN_PASSWORD_LENGTH: int = 8


class UserError(ValueError):
    """A rule violation the caller should report to the person, not a bug."""


class DuplicateUsername(UserError):
    pass


class LastAdmin(UserError):
    pass


def normalize_username(raw: str) -> str:
    return raw.strip().lower()


def validate_username(raw: str) -> str:
    username: str = normalize_username(raw)
    if not USERNAME_PATTERN.fullmatch(username):
        raise UserError("Username must be 3-32 characters: letters, digits, '.', '_' or '-'.")
    return username


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")


def validate_role(role: str) -> None:
    if role not in USER_ROLES:
        raise UserError(f"Role must be one of: {', '.join(USER_ROLES)}.")


def get(session: Session, user_id: str) -> User | None:
    return session.get(User, user_id)


def get_by_username(session: Session, raw_username: str) -> User | None:
    return session.scalars(select(User).where(User.username == normalize_username(raw_username))).one_or_none()


def list_users(session: Session) -> list[User]:
    return list(session.scalars(select(User).order_by(User.username)))


def create_user(session: Session, username: str, display_name: str, password: str, role: str) -> User:
    username = validate_username(username)
    validate_password(password)
    validate_role(role)
    if not display_name.strip():
        raise UserError("Display name is required.")
    if get_by_username(session, username) is not None:
        raise DuplicateUsername(f"Username '{username}' is already taken.")
    user: User = User(username=username, display_name=display_name.strip(), role=role,
                password_hash=hash_password(password))
    session.add(user)
    session.flush()
    categories.seed_defaults(session, user.id)  # every user starts with PRD §5.3's defaults
    return user


def set_password(session: Session, user: User, new_password: str) -> None:
    """Admin-performed reset (PRD §10.8). Ends every session the user has, everywhere."""
    validate_password(new_password)
    user.password_hash = hash_password(new_password)
    sessions.delete_all_for_user(session, user.id)


def set_role(session: Session, user: User, role: str) -> None:
    validate_role(role)
    if user.role == "admin" and role != "admin":
        admins: int = session.scalar(select(func.count()).select_from(User).where(User.role == "admin")) or 0
        if admins <= 1:
            raise LastAdmin("Cannot demote the last admin.")
    user.role = role
