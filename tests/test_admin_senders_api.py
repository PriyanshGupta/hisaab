"""/api/admin/senders: the sender review queue (PRD §6.10). Admin only; every sender here is invented."""

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Connection, select

from hisaab.models import Job, Sender, SenderCandidate
from tests import factories as f
from tests.conftest import login, make_user

ADMIN_PW: str = "admin password 1"
MEMBER_PW: str = "member password 1"
BANK: str = "alerts@harbour-bank.example"


@pytest.fixture
def admin_id(app: FastAPI) -> str:
    return make_user(app, "admin", ADMIN_PW, role="admin")


@pytest.fixture
def as_admin(client: TestClient, admin_id: str) -> TestClient:
    assert login(client, "admin", ADMIN_PW).status_code == 200
    return client


def example(subject: str, snippet: str = "Rs.10.00 has been debited") -> dict[str, str]:
    return {"subject": subject, "snippet": snippet, "received_at": "2026-10-18T04:30:00.000Z"}


def found(conn: Connection, user_id: str, address: str = BANK, count: int = 3, subjects: tuple[str, ...] = ("Debit alert",),
          sender: str | None = None) -> str:
    sid: str = sender or f.sender(conn, email_address=address, status="candidate")["id"]
    f.sender_candidate(conn, discovered_by_user_id=user_id, sender_address=address, match_count=count,
                       matched_subjects=json.dumps([example(s) for s in subjects]))
    conn.commit()
    return sid


def connected(conn: Connection, name: str) -> str:
    uid: str = f.user(conn, display_name=name)["id"]
    f.gmail_connection(conn, user_id=uid, email_address=f"{name.lower()}@example.test")
    conn.commit()
    return uid


def jobs(app: FastAPI, kind: str) -> list[Job]:
    with app.state.db.read_session() as s:
        rows: list[Job] = list(s.scalars(select(Job).where(Job.kind == kind)))
        s.expunge_all()
        return rows


def test_the_queue_shows_each_candidate_once_with_every_inbox_it_was_found_in(
        app: FastAPI, as_admin: TestClient, conn: Connection) -> None:
    asha, ravi = connected(conn, "Asha"), connected(conn, "Ravi")
    sid: str = found(conn, asha, count=4, subjects=("Debit alert", "UPI credited"))
    found(conn, ravi, count=1, sender=sid)
    f.sender(conn, email_address="news@kite-mart.example", status="ignored", reviewed_at="2026-10-19T08:00:00.000Z")
    conn.commit()
    body: dict[str, Any] = as_admin.get("/api/admin/senders").json()
    [item] = body["candidates"]
    assert (item["id"], item["address"]) == (sid, BANK)
    assert sorted((x["user"], x["match_count"]) for x in item["found_in"]) == [("Asha", 4), ("Ravi", 1)]
    asha_row: dict[str, Any] = next(x for x in item["found_in"] if x["user"] == "Asha")
    assert [e["subject"] for e in asha_row["examples"]] == ["Debit alert", "UPI credited"]
    assert body["ignored"] == [{"id": body["ignored"][0]["id"], "address": "news@kite-mart.example",
                                "reviewed_at": "2026-10-19T08:00:00.000Z"}]
    assert body["last_sweep_at"] is None


def test_approve_activates_for_everyone_discards_the_previews_and_backfills_every_inbox(
        app: FastAPI, as_admin: TestClient, conn: Connection) -> None:
    asha, ravi = connected(conn, "Asha"), connected(conn, "Ravi")
    sid: str = found(conn, asha)
    resp: Response = as_admin.post(f"/api/admin/senders/{sid}/approve")
    assert resp.status_code == 200 and resp.json() == {"address": BANK, "backfills": 2}
    with app.state.db.read_session() as s:
        sender: Sender = s.get_one(Sender, sid)
        candidate: SenderCandidate = s.scalars(select(SenderCandidate)).one()
        assert (sender.status, sender.reviewed_at is not None) == ("active", True)
        assert (candidate.discarded_at is not None, candidate.matched_subjects) == (True, "[]")
    backfills: list[Job] = jobs(app, "sender_backfill")
    assert sorted(j.user_id or "" for j in backfills) == sorted([asha, ravi])
    assert all(json.loads(j.params or "{}") == {"senders": [BANK]} for j in backfills)
    assert as_admin.get("/api/admin/senders").json()["candidates"] == []


def test_ignore_keeps_it_out_and_discards_its_previews(app: FastAPI, as_admin: TestClient, conn: Connection) -> None:
    sid: str = found(conn, connected(conn, "Asha"), address="news@kite-mart.example", subjects=("Cashback credited!",))
    assert as_admin.post(f"/api/admin/senders/{sid}/ignore").status_code == 204
    with app.state.db.read_session() as s:
        assert s.get_one(Sender, sid).status == "ignored"
        candidate: SenderCandidate = s.scalars(select(SenderCandidate)).one()
        assert (candidate.discarded_at is not None, candidate.matched_subjects) == (True, "[]")
    assert jobs(app, "sender_backfill") == []
    body: dict[str, Any] = as_admin.get("/api/admin/senders").json()
    assert body["candidates"] == [] and [x["address"] for x in body["ignored"]] == ["news@kite-mart.example"]


def test_only_a_waiting_candidate_can_be_approved_or_ignored(as_admin: TestClient, conn: Connection) -> None:
    active: str = f.sender(conn, email_address="alerts@known-bank.example", status="active")["id"]
    conn.commit()
    assert as_admin.post(f"/api/admin/senders/{active}/approve").status_code == 409
    assert as_admin.post(f"/api/admin/senders/{active}/ignore").status_code == 409
    assert as_admin.post("/api/admin/senders/nope/approve").status_code == 404


def test_run_sweep_now_queues_a_sweep_for_every_connected_inbox(
        app: FastAPI, as_admin: TestClient, conn: Connection) -> None:
    asha, ravi = connected(conn, "Asha"), connected(conn, "Ravi")
    f.gmail_connection(conn, status="needs_reauth")
    conn.commit()
    resp: Response = as_admin.post("/api/admin/senders/sweep")
    assert resp.status_code == 202 and len(resp.json()["jobs"]) == 2
    assert sorted(j.user_id or "" for j in jobs(app, "sweep")) == sorted([asha, ravi])


def test_the_last_sweep_time_is_shown(app: FastAPI, as_admin: TestClient, conn: Connection) -> None:
    f.job(conn, kind="sweep", state="succeeded", user_id=connected(conn, "Asha"),
          created_at="2026-10-18T04:00:00.000Z", finished_at="2026-10-18T04:01:00.000Z")
    conn.commit()
    assert as_admin.get("/api/admin/senders").json()["last_sweep_at"] == "2026-10-18T04:01:00.000Z"


def test_members_get_403_and_anonymous_401(app: FastAPI, client: TestClient, admin_id: str) -> None:
    make_user(app, "ravi", MEMBER_PW, role="member")
    routes: list[tuple[str, str]] = [("get", "/api/admin/senders"), ("post", "/api/admin/senders/x/approve"),
                                     ("post", "/api/admin/senders/x/ignore"), ("post", "/api/admin/senders/sweep")]
    for method, path in routes:
        assert getattr(client, method)(path).status_code == 401, path
    login(client, "ravi", MEMBER_PW)
    for method, path in routes:
        assert getattr(client, method)(path).status_code == 403, path
