"""/api/admin/payee-names: captured corrections, promote, dismiss (PRD §5.6). Admin only; invented data."""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Connection, func, select

from hisaab.models import Job, NormalizationPattern, Transaction
from tests import factories as f
from tests.conftest import login, make_user

ADMIN_PW: str = "admin password 1"
RAW: str = "UPI-CORNERGROCER-OKBANK"


@pytest.fixture
def admin_id(app: FastAPI) -> str:
    return make_user(app, "admin", ADMIN_PW, role="admin")


@pytest.fixture
def as_admin(client: TestClient, admin_id: str) -> TestClient:
    assert login(client, "admin", ADMIN_PW).status_code == 200
    return client


def seeded(conn: Connection) -> tuple[str, str, str]:
    """Asha corrected one Corner Grocer row; Ravi has two unnamed rows with the same text. (payee, asha, ravi)"""
    asha: str = f.user(conn, display_name="Asha")["id"]
    ravi: str = f.user(conn, display_name="Ravi")["id"]
    grocer: str = f.payee(conn, normalized_name="Corner Grocer")["id"]
    mine: str = f.transaction(conn, asha, payee_raw=RAW)["id"]
    f.transaction_edit(conn, mine, asha, field="payee_id", new_value=grocer)
    for _ in range(2):
        f.transaction(conn, ravi, payee_raw=RAW)
    conn.commit()
    return grocer, asha, ravi


def no_mail(*args: Any) -> tuple[int, bytes]:
    raise AssertionError("promoting a pattern must never reach Gmail")


def test_the_queue_shows_raw_text_name_shared_count_and_reporter(as_admin: TestClient, conn: Connection) -> None:
    grocer, _, _ = seeded(conn)
    [item] = as_admin.get("/api/admin/payee-names").json()
    assert {k: item[k] for k in ("raw", "payee_id", "payee_name", "shared")} == \
        {"raw": RAW, "payee_id": grocer, "payee_name": "Corner Grocer", "shared": 3}
    assert item["reporters"] == [{"user": "Asha", "count": 1}]


def test_promote_names_history_with_no_gmail_call_and_no_job(app: FastAPI, as_admin: TestClient,
                                                             conn: Connection) -> None:
    grocer, _, ravi = seeded(conn)
    app.state.mail_transport = no_mail
    with app.state.db.read_session() as s:
        jobs_before: int = s.scalar(select(func.count()).select_from(Job)) or 0
    resp: Response = as_admin.post("/api/admin/payee-names/promote", json={
        "payee_id": grocer, "match_type": "exact", "pattern": RAW})
    assert resp.status_code == 200 and {k: resp.json()[k] for k in ("named", "users")} == {"named": 3, "users": 2}
    with app.state.db.read_session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == jobs_before  # nothing queued, nothing fetched
        assert set(s.scalars(select(Transaction.payee_id).where(Transaction.user_id == ravi))) == {grocer}
        assert s.scalars(select(NormalizationPattern.origin)).all() == ["admin"]
    assert as_admin.get("/api/admin/payee-names").json() == []


def test_promote_with_a_new_name_and_an_unsafe_regex(as_admin: TestClient, conn: Connection) -> None:
    seeded(conn)
    ok: Response = as_admin.post("/api/admin/payee-names/promote", json={
        "payee_name": "Corner Grocery", "match_type": "contains", "pattern": "cornergrocer"})
    assert ok.status_code == 200 and ok.json()["named"] == 3
    bad: Response = as_admin.post("/api/admin/payee-names/promote", json={
        "payee_name": "X", "match_type": "regex", "pattern": "(?<=a)b"})
    assert bad.status_code == 422 and "regular expression" in bad.json()["detail"]


def test_dismiss_hides_the_row(as_admin: TestClient, conn: Connection) -> None:
    grocer, _, _ = seeded(conn)
    key: str = as_admin.get("/api/admin/payee-names").json()[0]["key"]
    assert as_admin.post("/api/admin/payee-names/dismiss", json={"key": key, "payee_id": grocer}).status_code == 204
    assert as_admin.get("/api/admin/payee-names").json() == []


def test_members_get_403_and_anonymous_401(app: FastAPI, client: TestClient, admin_id: str) -> None:
    make_user(app, "ravi", "member password 1", role="member")
    routes: list[tuple[str, str]] = [("get", "/api/admin/payee-names"), ("post", "/api/admin/payee-names/promote"),
                                     ("post", "/api/admin/payee-names/dismiss")]
    for method, path in routes:
        assert getattr(client, method)(path).status_code == 401, path
    login(client, "ravi", "member password 1")
    for method, path in routes:
        assert getattr(client, method)(path).status_code == 403, path
