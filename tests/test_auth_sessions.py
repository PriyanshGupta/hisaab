"""Sessions repository: hashed tokens, 30-day sliding expiry, throttled best-effort touch."""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, select
from sqlalchemy.orm import Session

from hisaab.auth import sessions
from hisaab.db import Database
from hisaab.models import UserSession
from tests import factories as f

SECRET: str = "test-session-secret-0123456789abcdef"


@pytest.fixture
def db(migrated_engine: Engine) -> Database:
    return Database(migrated_engine)


@pytest.fixture
def user_id(conn: Connection) -> str:
    uid: str = f.user(conn)["id"]
    conn.commit()
    return uid


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def set_last_seen(db: Database, token: str, when: datetime) -> None:
    with db.write_session() as s:
        row: UserSession = s.get_one(UserSession, sessions.token_id(SECRET, token))
        row.last_seen_at = iso(when)


def _resolved(s: Session, token: str) -> UserSession:
    """The session row for a token the test expects to be valid."""
    row: UserSession | None = sessions.resolve(s, SECRET, token)
    assert row is not None
    return row


def now() -> datetime:
    return datetime.now(UTC)


def test_created_token_resolves_to_its_user(db: Database, user_id: str) -> None:
    with db.write_session() as s:
        token: str = sessions.create(s, SECRET, user_id)
    with db.read_session() as s:
        assert _resolved(s, token).user_id == user_id


def test_token_is_stored_only_as_an_hmac(db: Database, user_id: str) -> None:
    with db.write_session() as s:
        token: str = sessions.create(s, SECRET, user_id)
    with db.read_session() as s:
        (stored,) = s.scalars(select(UserSession.id)).all()
    assert stored != token
    assert stored == hmac.new(SECRET.encode(), token.encode(), hashlib.sha256).hexdigest()


def test_token_is_long_and_random(db: Database, user_id: str) -> None:
    with db.write_session() as s:
        a, b = sessions.create(s, SECRET, user_id), sessions.create(s, SECRET, user_id)
    assert a != b and len(a) >= 43  # 32 random bytes, urlsafe base64


def test_a_different_secret_cannot_resolve_the_token(db: Database, user_id: str) -> None:
    with db.write_session() as s:
        token: str = sessions.create(s, SECRET, user_id)
    with db.read_session() as s:
        assert sessions.resolve(s, "another-secret-entirely-0123456789", token) is None


def test_unknown_token_resolves_to_none(db: Database) -> None:
    with db.read_session() as s:
        assert sessions.resolve(s, SECRET, "made-up-token") is None


def test_session_idle_just_under_30_days_is_valid(db: Database, user_id: str) -> None:
    with db.write_session() as s:
        token: str = sessions.create(s, SECRET, user_id)
    set_last_seen(db, token, now() - timedelta(days=29, hours=23))
    with db.read_session() as s:
        assert sessions.resolve(s, SECRET, token) is not None


def test_session_idle_over_30_days_is_rejected(db: Database, user_id: str) -> None:
    with db.write_session() as s:
        token: str = sessions.create(s, SECRET, user_id)
    set_last_seen(db, token, now() - timedelta(days=30, seconds=1))
    with db.read_session() as s:
        assert sessions.resolve(s, SECRET, token) is None


def test_purge_if_expired_deletes_the_row(db: Database, user_id: str) -> None:
    with db.write_session() as s:
        token: str = sessions.create(s, SECRET, user_id)
    set_last_seen(db, token, now() - timedelta(days=31))
    sessions.purge_if_expired(db, SECRET, token)
    with db.read_session() as s:
        assert s.scalars(select(UserSession)).all() == []


def test_touch_does_nothing_when_recently_seen(db: Database, user_id: str) -> None:
    with db.write_session() as s:
        token: str = sessions.create(s, SECRET, user_id)
    with db.read_session() as s:
        before: str = _resolved(s, token).last_seen_at
    assert sessions.touch_if_due(db, SECRET, token, before) is False
    with db.read_session() as s:
        assert _resolved(s, token).last_seen_at == before


def test_touch_slides_expiry_when_due(db: Database, user_id: str) -> None:
    with db.write_session() as s:
        token: str = sessions.create(s, SECRET, user_id)
    stale: str = iso(now() - timedelta(minutes=6))
    set_last_seen(db, token, now() - timedelta(minutes=6))
    assert sessions.touch_if_due(db, SECRET, token, stale) is True
    with db.read_session() as s:
        assert _resolved(s, token).last_seen_at > stale


def test_touch_is_skipped_without_waiting_while_writer_is_busy(db: Database, user_id: str) -> None:
    with db.write_session() as s:
        token: str = sessions.create(s, SECRET, user_id)
    stale: str = iso(now() - timedelta(days=1))
    set_last_seen(db, token, now() - timedelta(days=1))
    with db.writer_lock:  # a long job holds the writer
        assert sessions.touch_if_due(db, SECRET, token, stale) is False
    with db.read_session() as s:
        assert _resolved(s, token).last_seen_at == stale


def test_delete_removes_one_session(db: Database, user_id: str) -> None:
    with db.write_session() as s:
        keep, drop = sessions.create(s, SECRET, user_id), sessions.create(s, SECRET, user_id)
    with db.write_session() as s:
        sessions.delete(s, SECRET, drop)
    with db.read_session() as s:
        assert sessions.resolve(s, SECRET, drop) is None
        assert sessions.resolve(s, SECRET, keep) is not None


def test_delete_all_for_user_spares_other_users(db: Database, conn: Connection, user_id: str) -> None:
    other: str = f.user(conn)["id"]
    conn.commit()
    with db.write_session() as s:
        mine: list[str] = [sessions.create(s, SECRET, user_id) for _ in range(2)]
        theirs: str = sessions.create(s, SECRET, other)
    with db.write_session() as s:
        sessions.delete_all_for_user(s, user_id)
    with db.read_session() as s:
        assert all(sessions.resolve(s, SECRET, t) is None for t in mine)
        assert sessions.resolve(s, SECRET, theirs) is not None
