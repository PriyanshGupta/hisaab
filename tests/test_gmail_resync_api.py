"""/api/gmail/resync: anyone syncs a date range of their own inbox, from Settings (user, 2026-10-21; it was the
admin's tool only). Always the caller's inbox, at most about six months, never past today, one at a time, a backup
first."""

import json
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Connection, select, update

from hisaab.clock import local_today
from hisaab.models import Job
from tests import factories as f
from tests.conftest import login, make_user

PW: str = "member password 1"


@pytest.fixture
def ravi(app: FastAPI) -> str:
    return make_user(app, "ravi", PW)  # a member


@pytest.fixture
def as_ravi(client: TestClient, ravi: str) -> TestClient:
    assert login(client, "ravi", PW).status_code == 200
    return client


def connect(conn: Connection, user_id: str, *received: str) -> None:
    f.gmail_connection(conn, user_id=user_id, email_address=f"{user_id[:6]}@example.test")
    for when in received:
        src: str = f.source(conn, user_id, kind="email", received_at=when)["id"]
        f.transaction_source(conn, f.transaction(conn, user_id, primary_source_id=src)["id"], src)
    conn.commit()


def resync_jobs(app: FastAPI) -> list[Job]:
    with app.state.db.read_session() as s:
        jobs: list[Job] = list(s.scalars(select(Job).where(Job.kind == "resync")))
        s.expunge_all()
        return jobs


def test_a_member_previews_and_syncs_their_own_inbox_only(app: FastAPI, as_ravi: TestClient, ravi: str,
                                                          conn: Connection) -> None:
    connect(conn, ravi, "2026-10-06T04:00:00.000Z")
    other: str = f.user(conn)["id"]
    connect(conn, other, "2026-10-06T05:00:00.000Z")  # someone else's mail in the same days: not counted
    preview: Response = as_ravi.get("/api/gmail/resync/preview", params={"start": "2026-10-05", "end": "2026-10-07"})
    assert preview.json() == {"transactions": 1}
    started: Response = as_ravi.post("/api/gmail/resync", json={"start": "2026-10-05", "end": "2026-10-07"})
    assert started.status_code == 202
    [job] = resync_jobs(app)
    assert (job.id, job.user_id, json.loads(job.params or "{}")) == (
        started.json()["job_id"], ravi, {"from": "2026-10-05", "to": "2026-10-07"})


def test_someone_elses_inbox_cannot_be_asked_for(app: FastAPI, as_ravi: TestClient, ravi: str,
                                                 conn: Connection) -> None:
    connect(conn, ravi)
    other: str = f.user(conn)["id"]
    connect(conn, other)
    as_ravi.post("/api/gmail/resync", json={"start": "2026-10-05", "end": "2026-10-07", "user_id": other})
    assert [j.user_id for j in resync_jobs(app)] == [ravi]


def test_a_backup_is_taken_first(app: FastAPI, as_ravi: TestClient, ravi: str, conn: Connection) -> None:
    connect(conn, ravi)
    as_ravi.post("/api/gmail/resync", json={"start": "2026-10-05", "end": "2026-10-07"})
    assert [p.name for p in app.state.settings.backup_dir.glob("*-pre-resync.db")] != []


def test_one_at_a_time(as_ravi: TestClient, ravi: str, conn: Connection) -> None:
    connect(conn, ravi)
    running: str = f.job(conn, kind="resync", user_id=ravi, state="running")["id"]
    f.job(conn, kind="resync", user_id=f.user(conn)["id"], state="running")  # someone else's doesn't block
    conn.commit()
    again: Response = as_ravi.post("/api/gmail/resync", json={"start": "2026-09-01", "end": "2026-09-07"})
    assert again.status_code == 409
    assert "already" in again.json()["detail"]
    conn.execute(update(Job).where(Job.id == running).values(state="succeeded"))
    conn.commit()
    assert as_ravi.post("/api/gmail/resync", json={"start": "2026-09-01", "end": "2026-09-07"}).status_code == 202


@pytest.mark.parametrize(("start", "end", "says"), [
    ("2026-10-07", "2026-10-05", "ends before it starts"),
    ("2026-01-01", "2026-09-30", "6 months"),
    ("bad", "2026-10-05", "2026-10-05"),
])
def test_a_range_that_cant_be_synced_is_refused(as_ravi: TestClient, ravi: str, conn: Connection,
                                                start: str, end: str, says: str) -> None:
    connect(conn, ravi)
    for resp in (as_ravi.get("/api/gmail/resync/preview", params={"start": start, "end": end}),
                 as_ravi.post("/api/gmail/resync", json={"start": start, "end": end})):
        assert resp.status_code == 422
        assert says in resp.json()["detail"]


def test_a_range_past_today_is_refused(as_ravi: TestClient, ravi: str, conn: Connection) -> None:
    connect(conn, ravi)
    tomorrow: str = (local_today() + timedelta(days=1)).isoformat()
    resp: Response = as_ravi.post("/api/gmail/resync", json={"start": local_today().isoformat(), "end": tomorrow})
    assert resp.status_code == 422
    assert "future" in resp.json()["detail"]


def test_without_gmail_connected_it_is_refused(app: FastAPI, as_ravi: TestClient) -> None:
    resp: Response = as_ravi.post("/api/gmail/resync", json={"start": "2026-10-05", "end": "2026-10-07"})
    assert resp.status_code == 409
    assert resync_jobs(app) == []


def test_anonymous_gets_401(client: TestClient) -> None:
    assert client.post("/api/gmail/resync", json={"start": "2026-10-05", "end": "2026-10-07"}).status_code == 401
