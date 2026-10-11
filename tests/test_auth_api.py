"""Login, logout, /me, cookie flags, sliding expiry over HTTP."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hisaab.auth import sessions
from hisaab.config import Settings
from hisaab.main import create_app
from hisaab.models import UserSession
from tests.conftest import login, make_user

THIRTY_DAYS = 30 * 24 * 3600


@pytest.fixture
def asha(app):
    return make_user(app, "asha", "correct horse battery", role="admin", display_name="Asha")


def _cookie_header(resp):
    (header,) = [v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie"]
    return header


def _age_all_sessions(app, delta):
    when = datetime.now(UTC) - delta
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"
    with app.state.db.write_session() as s:
        for row in s.scalars(select(UserSession)):
            row.last_seen_at = stamp


def test_login_sets_an_httponly_samesite_lax_cookie(client, asha):
    resp = login(client, "asha", "correct horse battery")
    assert resp.status_code == 200
    cookie = _cookie_header(resp)
    attrs = {a.strip().split("=")[0].lower() for a in cookie.split(";")}
    assert cookie.startswith("hisaab_session=")
    assert "httponly" in attrs
    assert "samesite=lax" in cookie.lower()
    assert f"max-age={THIRTY_DAYS}" in cookie.lower()
    assert "path=/" in cookie.lower()
    assert "secure" not in attrs  # plain HTTP on the LAN (tech-design §9.6)


def test_me_returns_the_logged_in_user(client, asha):
    login(client, "asha", "correct horse battery")
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.json() == {"id": asha, "username": "asha", "display_name": "Asha", "role": "admin"}


def test_username_is_case_insensitive(client, asha):
    assert login(client, "  ASHA ", "correct horse battery").status_code == 200


def test_wrong_password_and_unknown_user_are_indistinguishable(client, asha):
    wrong = login(client, "asha", "wrong password!")
    unknown = login(client, "nobody", "wrong password!")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert "set-cookie" not in wrong.headers


def test_me_without_cookie_is_401(client):
    assert client.get("/api/auth/me").status_code == 401


def test_me_with_forged_cookie_is_401(client, asha):
    client.cookies.set("hisaab_session", "forged-token")
    assert client.get("/api/auth/me").status_code == 401


def test_logout_ends_the_session(app, client, asha):
    login(client, "asha", "correct horse battery")
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 204
    assert 'hisaab_session=""' in _cookie_header(resp) or "max-age=0" in _cookie_header(resp).lower()
    with app.state.db.read_session() as s:
        assert s.scalars(select(UserSession)).all() == []
    assert client.get("/api/auth/me").status_code == 401


def test_logout_without_session_is_harmless(client):
    assert client.post("/api/auth/logout").status_code == 204


def test_session_idle_over_30_days_is_rejected_and_removed(app, client, asha):
    login(client, "asha", "correct horse battery")
    _age_all_sessions(app, timedelta(days=30, minutes=1))
    assert client.get("/api/auth/me").status_code == 401
    with app.state.db.read_session() as s:
        assert s.scalars(select(UserSession)).all() == []


def test_session_idle_29_days_still_works_and_slides(app, client, asha):
    login(client, "asha", "correct horse battery")
    _age_all_sessions(app, timedelta(days=29))
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    assert f"max-age={THIRTY_DAYS}" in _cookie_header(resp).lower()  # cookie re-issued: expiry slid
    with app.state.db.read_session() as s:
        (last_seen_at,) = s.scalars(select(UserSession.last_seen_at)).all()
    assert sessions._age(last_seen_at) < timedelta(minutes=1)


def test_recent_activity_does_not_rewrite_the_cookie(client, asha):
    login(client, "asha", "correct horse battery")
    resp = client.get("/api/auth/me")
    assert "set-cookie" not in resp.headers  # touched < 5 min ago: no write, no new cookie


def test_requests_are_served_while_a_job_holds_the_writer(app, client, asha):
    login(client, "asha", "correct horse battery")
    _age_all_sessions(app, timedelta(days=1))
    with app.state.db.writer_lock:  # a long job is writing
        assert client.get("/api/auth/me").status_code == 200


def test_login_returns_503_when_writer_is_busy(app, client, asha):
    app.state.writer_timeout = 0.05
    with app.state.db.writer_lock:
        resp = login(client, "asha", "correct horse battery")
    assert resp.status_code == 503


def test_outdated_password_hash_is_upgraded_on_login(app, client, asha):
    from argon2 import PasswordHasher

    from hisaab.models import User

    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("correct horse battery")
    with app.state.db.write_session() as s:
        s.get(User, asha).password_hash = weak
    assert login(client, "asha", "correct horse battery").status_code == 200
    with app.state.db.read_session() as s:
        assert s.get(User, asha).password_hash != weak


def test_health_stays_public(client):
    assert client.get("/health").status_code == 200


def test_app_refuses_to_start_without_session_secret(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "x.db")
    with pytest.raises(RuntimeError, match="HISAAB_SESSION_SECRET"):
        create_app(settings)


def test_app_refuses_a_short_session_secret(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "x.db", session_secret="short")
    with pytest.raises(RuntimeError, match="at least 32"):
        create_app(settings)
