"""/api/gmail, /oauth/callback and /api/jobs (tech-design §5.5, §6.7), against a fake Google."""

import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import SecretStr
from sqlalchemy import select

from hisaab.api.gmail import sign_state
from hisaab.config import Settings
from hisaab.ingest.sync import make_sync
from hisaab.jobs.queue import JobKind
from hisaab.main import create_app
from hisaab.models import GmailConnection, Job
from tests import factories as f
from tests.conftest import login, make_user
from tests.gmail_fake import FakeGmail

KEY: str = Fernet.generate_key().decode()


@pytest.fixture
def fake() -> FakeGmail:
    return FakeGmail()


@pytest.fixture
def gmail_app(settings: Settings, migrated_engine: Any, fake: FakeGmail) -> FastAPI:
    configured: Settings = settings.model_copy(update={
        "google_oauth_client_id": "client-id", "google_oauth_client_secret": SecretStr("client-secret"),
        "encryption_key": SecretStr(KEY)})
    app: FastAPI = create_app(configured)
    app.state.mail_transport = fake
    app.state.jobs.kinds["sync"] = JobKind(run=make_sync(configured, http=fake, sleep=lambda s: None), timeout=60,
                                           joins=True)
    return app


@pytest.fixture
def asha(gmail_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(gmail_app) as client:
        make_user(gmail_app, "asha", "asha password 1")
        login(client, "asha", "asha password 1")
        yield client


def connect(client: TestClient) -> Response:
    url: str = client.post("/api/gmail/connect").json()["authorization_url"]
    state: str = parse_qs(urlsplit(url).query)["state"][0]
    client.cookies.clear()  # the callback must not depend on the session cookie (LAN address vs localhost)
    return client.get("/oauth/callback", params={"code": "auth-code", "state": state}, follow_redirects=False)


def wait_job(client: TestClient, job_id: str) -> dict[str, Any]:
    deadline: float = time.monotonic() + 10
    while time.monotonic() < deadline:
        job: dict[str, Any] = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] not in ("queued", "running"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_status_before_connecting(asha: TestClient) -> None:
    assert asha.get("/api/gmail").json() == {"configured": True, "status": "not_connected", "email_address": None,
                                             "last_successful_sync_at": None}


def test_unconfigured_gmail_says_what_to_set(app: FastAPI) -> None:
    with TestClient(app) as client:
        make_user(app, "asha", "asha password 1")
        login(client, "asha", "asha password 1")
        assert client.get("/api/gmail").json()["configured"] is False
        resp: Response = client.post("/api/gmail/connect")
    assert resp.status_code == 409 and "HISAAB_GOOGLE_OAUTH_CLIENT_ID" in resp.json()["detail"]


def test_connect_round_trip_stores_the_grant_and_starts_a_sync(asha: TestClient, gmail_app: FastAPI) -> None:
    resp: Response = connect(asha)
    assert resp.status_code == 303 and resp.headers["location"] == "/settings?gmail=connected"
    login(asha, "asha", "asha password 1")
    status: dict[str, Any] = asha.get("/api/gmail").json()
    assert (status["status"], status["email_address"]) == ("connected", "asha@example.test")
    with gmail_app.state.db.read_session() as s:
        assert b"fake-refresh-token" not in s.scalars(select(GmailConnection.refresh_token_enc)).one()
        job_id: str = s.scalars(select(Job.id).where(Job.kind == "sync")).one()
    assert wait_job(asha, job_id)["state"] == "succeeded"


def test_a_tampered_state_is_refused(asha: TestClient, gmail_app: FastAPI) -> None:
    url: str = asha.post("/api/gmail/connect").json()["authorization_url"]
    state: str = parse_qs(urlsplit(url).query)["state"][0]
    resp: Response = asha.get("/oauth/callback", params={"code": "c", "state": state[:-1] + "x"}, follow_redirects=False)
    assert resp.headers["location"] == "/settings?gmail=error"
    with gmail_app.state.db.read_session() as s:
        assert s.scalars(select(GmailConnection)).all() == []


def test_an_expired_state_is_refused(asha: TestClient, gmail_app: FastAPI) -> None:
    user_id: str = asha.get("/api/auth/me").json()["id"]
    state: str = sign_state(gmail_app.state.settings.session_secret.get_secret_value(), user_id, ttl_s=-1)
    resp: Response = asha.get("/oauth/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert resp.headers["location"] == "/settings?gmail=error"


def test_declining_consent_comes_back_as_denied(asha: TestClient) -> None:
    resp: Response = asha.get("/oauth/callback", params={"error": "access_denied", "state": "x"}, follow_redirects=False)
    assert resp.headers["location"] == "/settings?gmail=denied"


def test_sync_now_returns_a_job_to_poll(asha: TestClient) -> None:
    connect(asha)
    login(asha, "asha", "asha password 1")
    job_id: str = asha.post("/api/gmail/sync").json()["job_id"]
    job: dict[str, Any] = wait_job(asha, job_id)
    assert (job["kind"], job["state"], job["error"]) == ("sync", "succeeded", None)
    assert set(job["result"]) >= {"stored", "already_had", "failed"}


def test_sync_now_without_a_connection_is_refused(asha: TestClient) -> None:
    resp: Response = asha.post("/api/gmail/sync")
    assert resp.status_code == 409 and "Connect Gmail" in resp.json()["detail"]


def test_a_job_is_visible_only_to_its_owner(asha: TestClient, gmail_app: FastAPI) -> None:
    with gmail_app.state.db.write_session() as s:
        other: str = f.user(s.connection())["id"]
        f.job(s.connection(), id="theirs", user_id=other, created_at="2026-10-11T00:00:00.000Z")
    assert asha.get("/api/jobs/theirs").status_code == 404
    asha.cookies.clear()
    assert asha.get("/api/jobs/theirs").status_code == 401
