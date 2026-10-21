"""/api/admin/resync: preview the count, start a re-sync, and the parser-change notices (tech-design §5.2)."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Connection, select

from hisaab.models import Job, ResyncNotice
from tests import factories as f
from tests.conftest import login, make_user

ADMIN_PW: str = "admin password 1"


@pytest.fixture
def admin_id(app: FastAPI) -> str:
    return make_user(app, "admin", ADMIN_PW, role="admin")


@pytest.fixture
def as_admin(client: TestClient, admin_id: str) -> TestClient:
    assert login(client, "admin", ADMIN_PW).status_code == 200
    return client


def inbox(conn: Connection, name: str) -> str:
    uid: str = f.user(conn, display_name=name)["id"]
    f.gmail_connection(conn, user_id=uid, email_address=f"{name.lower()}@example.test")
    for when in ("2026-10-06T04:00:00.000Z", "2026-09-01T04:00:00.000Z"):
        src: str = f.source(conn, uid, kind="email", received_at=when)["id"]
        f.transaction_source(conn, f.transaction(conn, uid, primary_source_id=src)["id"], src)
    conn.commit()
    return uid


def test_preview_names_the_count_for_all_inboxes_or_one(as_admin: TestClient, conn: Connection) -> None:
    asha, _ = inbox(conn, "Asha"), inbox(conn, "Ravi")
    every: Response = as_admin.get("/api/admin/resync/preview", params={"start": "2026-10-05", "end": "2026-10-07"})
    assert every.json() == {"transactions": 2, "inboxes": 2}
    one: Response = as_admin.get("/api/admin/resync/preview",
                                 params={"start": "2026-10-05", "end": "2026-10-07", "user_id": asha})
    assert one.json() == {"transactions": 1, "inboxes": 1}


def test_a_backwards_range_is_refused(as_admin: TestClient) -> None:
    assert as_admin.get("/api/admin/resync/preview", params={"start": "2026-10-07", "end": "2026-10-05"}).status_code == 422
    assert as_admin.post("/api/admin/resync", json={"start": "2026-10-07", "end": "2026-10-05"}).status_code == 422


def test_starting_queues_resync_jobs_and_resolves_covered_notices(app: FastAPI, as_admin: TestClient,
                                                                  conn: Connection) -> None:
    asha: str = inbox(conn, "Asha")
    notice: str = f.resync_notice(conn, range_from="2026-10-05", range_to="2026-10-06")["id"]
    conn.commit()
    resp: Response = as_admin.post("/api/admin/resync", json={"start": "2026-10-05", "end": "2026-10-07"})
    assert resp.status_code == 202 and len(resp.json()["jobs"]) == 1
    with app.state.db.read_session() as s:
        job: Job = s.scalars(select(Job).where(Job.kind == "resync")).one()
        assert (job.user_id, json.loads(job.params or "{}")) == (asha, {"from": "2026-10-05", "to": "2026-10-07"})
        assert s.get_one(ResyncNotice, notice).resolved_at is not None


def test_notices_are_listed_newest_first_and_can_be_dismissed(as_admin: TestClient, conn: Connection) -> None:
    old: str = f.resync_notice(conn, template_name="old_one", created_at="2026-10-01T00:00:00.000Z")["id"]
    f.resync_notice(conn, template_name="new_one", reason="approved", created_at="2026-10-20T00:00:00.000Z")
    f.resync_notice(conn, template_name="done", resolved_at="2026-10-20T00:00:00.000Z")
    conn.commit()
    listed: list[dict[str, str]] = as_admin.get("/api/admin/resync/notices").json()
    assert [(n["template_name"], n["reason"]) for n in listed] == [("new_one", "approved"), ("old_one", "edited")]
    assert set(listed[0]) == {"id", "template_name", "reason", "range_from", "range_to", "created_at"}
    assert as_admin.post(f"/api/admin/resync/notices/{old}/dismiss").status_code == 204
    assert [n["template_name"] for n in as_admin.get("/api/admin/resync/notices").json()] == ["new_one"]
    assert as_admin.post("/api/admin/resync/notices/nope/dismiss").status_code == 404


def test_members_get_403_and_anonymous_401(app: FastAPI, client: TestClient, admin_id: str) -> None:
    make_user(app, "ravi", "member password 1", role="member")
    routes: list[tuple[str, str]] = [("get", "/api/admin/resync/preview"), ("post", "/api/admin/resync"),
                                     ("get", "/api/admin/resync/notices"), ("post", "/api/admin/resync/notices/x/dismiss")]
    for method, path in routes:
        assert getattr(client, method)(path).status_code == 401, path
    login(client, "ravi", "member password 1")
    for method, path in routes:
        assert getattr(client, method)(path).status_code == 403, path


def test_a_resync_of_one_inbox_leaves_the_global_notices_open(app: FastAPI, as_admin: TestClient,
                                                              conn: Connection) -> None:
    """Slice 24 review: a template is global, so its notice covers every inbox's emails in that range."""
    asha: str = inbox(conn, "Asha")
    inbox(conn, "Ravi")
    notice: str = f.resync_notice(conn, range_from="2026-10-05", range_to="2026-10-06")["id"]
    conn.commit()
    as_admin.post("/api/admin/resync", json={"start": "2026-10-05", "end": "2026-10-07", "user_id": asha})
    with app.state.db.read_session() as s:
        assert s.get_one(ResyncNotice, notice).resolved_at is None
