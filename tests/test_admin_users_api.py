"""Admin-only user management: create, list, reset password, change role."""

import pytest
from sqlalchemy import func, select

from hisaab.models import UserSession
from tests.conftest import login, make_user

ADMIN_PW = "admin password 1"
MEMBER_PW = "member password 1"


@pytest.fixture
def admin_id(app):
    return make_user(app, "admin", ADMIN_PW, role="admin")


@pytest.fixture
def member_id(app):
    return make_user(app, "ravi", MEMBER_PW, role="member")


@pytest.fixture
def as_admin(client, admin_id):
    assert login(client, "admin", ADMIN_PW).status_code == 200
    return client


def _admin_routes(target_id):
    return [
        ("get", "/api/admin/users", None),
        ("post", "/api/admin/users", {"username": "x-user", "display_name": "X", "password": "password123", "role": "member"}),
        ("post", f"/api/admin/users/{target_id}/password", {"password": "password123"}),
        ("patch", f"/api/admin/users/{target_id}", {"role": "admin"}),
    ]


def test_admin_creates_a_member_who_can_then_log_in(as_admin):
    resp = as_admin.post("/api/admin/users", json={
        "username": "Meera", "display_name": "Meera", "password": "member pass 99", "role": "member",
    })
    assert resp.status_code == 201
    assert resp.json()["username"] == "meera" and resp.json()["role"] == "member"
    as_admin.cookies.clear()
    assert login(as_admin, "meera", "member pass 99").status_code == 200


def test_duplicate_username_is_409(as_admin, member_id):
    resp = as_admin.post("/api/admin/users", json={
        "username": "RAVI", "display_name": "Other Ravi", "password": "password123", "role": "member",
    })
    assert resp.status_code == 409


@pytest.mark.parametrize("body", [
    {"username": "ab", "display_name": "Too short", "password": "password123", "role": "member"},
    {"username": "has space", "display_name": "Bad chars", "password": "password123", "role": "member"},
    {"username": "okname", "display_name": "Short pw", "password": "short", "role": "member"},
    {"username": "okname", "display_name": "Bad role", "password": "password123", "role": "owner"},
    {"username": "okname", "display_name": "   ", "password": "password123", "role": "member"},
])
def test_invalid_new_user_is_422(as_admin, body):
    assert as_admin.post("/api/admin/users", json=body).status_code == 422


def test_member_is_forbidden_from_every_admin_route(client, admin_id, member_id):
    login(client, "ravi", MEMBER_PW)
    for method, path, body in _admin_routes(admin_id):
        resp = getattr(client, method)(path, json=body) if body else getattr(client, method)(path)
        assert resp.status_code == 403, (method, path)


def test_anonymous_gets_401_on_every_admin_route(client, admin_id):
    for method, path, body in _admin_routes(admin_id):
        resp = getattr(client, method)(path, json=body) if body else getattr(client, method)(path)
        assert resp.status_code == 401, (method, path)


def test_list_users_never_exposes_password_hashes(as_admin, member_id):
    resp = as_admin.get("/api/admin/users")
    assert resp.status_code == 200
    assert [u["username"] for u in resp.json()] == ["admin", "ravi"]
    assert all(set(u) == {"id", "username", "display_name", "role", "created_at"} for u in resp.json())


def test_password_reset_replaces_password_and_ends_target_sessions(app, client, admin_id, member_id):
    login(client, "ravi", MEMBER_PW)  # ravi has a live session
    client.cookies.clear()
    login(client, "admin", ADMIN_PW)
    resp = client.post(f"/api/admin/users/{member_id}/password", json={"password": "brand new pass"})
    assert resp.status_code == 204
    with app.state.db.read_session() as s:
        ravi_sessions = s.scalar(select(func.count()).select_from(UserSession).where(UserSession.user_id == member_id))
    assert ravi_sessions == 0
    client.cookies.clear()
    assert login(client, "ravi", MEMBER_PW).status_code == 401
    assert login(client, "ravi", "brand new pass").status_code == 200


def test_password_reset_rejects_short_password(as_admin, member_id):
    assert as_admin.post(f"/api/admin/users/{member_id}/password", json={"password": "short"}).status_code == 422


def test_unknown_user_is_404(as_admin):
    assert as_admin.post("/api/admin/users/no-such-id/password", json={"password": "password123"}).status_code == 404
    assert as_admin.patch("/api/admin/users/no-such-id", json={"role": "admin"}).status_code == 404


def test_promote_member_to_admin(client, as_admin, member_id):
    resp = as_admin.patch(f"/api/admin/users/{member_id}", json={"role": "admin"})
    assert resp.status_code == 200 and resp.json()["role"] == "admin"
    client.cookies.clear()
    login(client, "ravi", MEMBER_PW)
    assert client.get("/api/admin/users").status_code == 200  # role takes effect


def test_last_admin_cannot_be_demoted(as_admin, admin_id):
    resp = as_admin.patch(f"/api/admin/users/{admin_id}", json={"role": "member"})
    assert resp.status_code == 409


def test_admin_can_be_demoted_when_another_admin_exists(as_admin, admin_id, member_id):
    as_admin.patch(f"/api/admin/users/{member_id}", json={"role": "admin"})
    resp = as_admin.patch(f"/api/admin/users/{admin_id}", json={"role": "member"})
    assert resp.status_code == 200
    assert as_admin.get("/api/admin/users").status_code == 403  # demotion applies immediately
