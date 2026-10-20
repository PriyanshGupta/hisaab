"""/api/admin/templates: the promotion queue (PRD §6.8). Admin only; every email here is invented."""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import SecretStr
from sqlalchemy import select

from hisaab.config import Settings
from hisaab.db import Database
from hisaab.jobs.queue import JobContext
from hisaab.models import ParserTemplate
from hisaab.promotion.propose import propose_ready
from tests import factories as f
from tests.claude_fake import FakeClaude
from tests.conftest import login, make_user
from tests.promotion_helpers import Haiku, alert_eml, feed, reading
from tests.test_propose import ANSWER, WRONG

ADMIN_PW: str = "admin password 1"
MEMBER_PW: str = "member password 1"


@pytest.fixture
def admin_id(app: FastAPI) -> str:
    return make_user(app, "admin", ADMIN_PW, role="admin")


@pytest.fixture
def as_admin(client: TestClient, admin_id: str) -> TestClient:
    assert login(client, "admin", ADMIN_PW).status_code == 200
    return client


def candidate(app: FastAPI, user_id: str, answer: dict[str, Any] = ANSWER) -> str:
    db: Database = app.state.db
    feed(db, user_id, [alert_eml(n) for n in range(3)], Haiku(*[reading(n) for n in range(3)]))
    settings: Settings = app.state.settings.model_copy(update={"anthropic_api_key": SecretStr("test-key")})
    propose_ready(JobContext(db, "job", user_id, {}), settings, FakeClaude(reply=lambda prompt: answer), lambda s: None)
    with db.read_session() as s:
        template_id: str | None = s.scalar(select(ParserTemplate.id).where(ParserTemplate.origin == "llm_proposed"))
    assert template_id is not None
    return template_id


def test_the_queue_shows_the_candidate_with_its_emails_and_replay(app: FastAPI, as_admin: TestClient,
                                                                  admin_id: str) -> None:
    template_id: str = candidate(app, admin_id)
    body: dict[str, Any] = as_admin.get("/api/admin/templates").json()
    [item] = body["candidates"]
    assert (item["id"], item["name"], item["status"], item["passed"]) == \
        (template_id, "lantern_upi_debit", "validated", True)
    assert (item["sender"], item["subject_masked"], item["sightings"]) == \
        ("alerts@lantern.example", "You have done a UPI txn", 3)
    assert item["spec"]["match"]["sender"] == "alerts@lantern.example"
    assert [e["subject"] for e in item["examples"]] == ["You have done a UPI txn"] * 3
    assert "Rs.89.50 has been debited" in item["examples"][1]["text"]  # the text the patterns read
    amount: dict[str, Any] = next(r for r in item["examples"][1]["fields"] if r["field"] == "amount")
    assert amount == {"field": "amount", "expected": 8950, "got": 8950, "match": True}
    assert body["demoted"] == []


def test_a_failed_candidate_is_listed_and_cannot_be_approved(app: FastAPI, as_admin: TestClient,
                                                             admin_id: str) -> None:
    template_id: str = candidate(app, admin_id, WRONG)
    [item] = as_admin.get("/api/admin/templates").json()["candidates"]
    assert item["passed"] is False and "email 1: reference_number differs" in item["problems"]
    assert as_admin.post(f"/api/admin/templates/{template_id}/approve").status_code == 409


def test_approve_returns_what_it_did(app: FastAPI, as_admin: TestClient, admin_id: str) -> None:
    template_id: str = candidate(app, admin_id)
    resp: Response = as_admin.post(f"/api/admin/templates/{template_id}/approve")
    assert resp.status_code == 200
    assert {k: resp.json()[k] for k in ("reread", "waiting", "invalidated", "flagged")} == \
        {"reread": 3, "waiting": 0, "invalidated": 3, "flagged": 0}
    assert len(resp.json()["exported"]) == 3
    assert as_admin.get("/api/admin/templates").json()["candidates"] == []


def test_reject_is_204_and_leaves_a_discarded_candidate(app: FastAPI, as_admin: TestClient, admin_id: str) -> None:
    template_id: str = candidate(app, admin_id)
    assert as_admin.post(f"/api/admin/templates/{template_id}/reject").status_code == 204
    [item] = as_admin.get("/api/admin/templates").json()["candidates"]
    assert item["passed"] is False and item["problems"][-1] == "Rejected by an admin"


def test_an_unknown_template_is_404(as_admin: TestClient) -> None:
    assert as_admin.post("/api/admin/templates/nope/approve").status_code == 404
    assert as_admin.post("/api/admin/templates/nope/reject").status_code == 404


def test_demoted_templates_are_listed(app: FastAPI, as_admin: TestClient) -> None:
    with app.state.db.write_session() as s:
        s.add(ParserTemplate(artifact_type="email", name="old_format", spec="{}", origin="human", status="demoted",
                             consecutive_failures=3, demoted_at="2026-10-19T08:00:00.000Z"))
    assert as_admin.get("/api/admin/templates").json()["demoted"] == [
        {"id": as_admin.get("/api/admin/templates").json()["demoted"][0]["id"], "name": "old_format",
         "demoted_at": "2026-10-19T08:00:00.000Z", "consecutive_failures": 3}]


def test_members_get_403_and_anonymous_401(app: FastAPI, client: TestClient, admin_id: str) -> None:
    make_user(app, "ravi", MEMBER_PW, role="member")
    routes: list[tuple[str, str]] = [("get", "/api/admin/templates"), ("post", "/api/admin/templates/x/approve"),
                                     ("post", "/api/admin/templates/x/reject")]
    for method, path in routes:
        assert getattr(client, method)(path).status_code == 401, path
    login(client, "ravi", MEMBER_PW)
    for method, path in routes:
        assert getattr(client, method)(path).status_code == 403, path


def test_excluded_formats_are_listed_and_can_be_read_again(app: FastAPI, as_admin: TestClient, admin_id: str) -> None:
    from tests.promotion_helpers import MARKETING_READING, offer_eml
    feed(app.state.db, admin_id, [offer_eml(0), offer_eml(1)], Haiku(MARKETING_READING, MARKETING_READING))
    [item] = as_admin.get("/api/admin/templates").json()["excluded"]
    assert (item["sender"], item["sightings"]) == ("alerts@lantern.example", 2)
    assert as_admin.post(f"/api/admin/templates/formats/{item['id']}/include").status_code == 204
    assert as_admin.get("/api/admin/templates").json()["excluded"] == []
    assert as_admin.post(f"/api/admin/templates/formats/{item['id']}/include").status_code == 404
