"""/api/statements: upload with a password, the preview, picking the account, discard, merge (tech-design §7)."""

import base64
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import func, select

from hisaab.config import Settings
from hisaab.db import Database
from hisaab.models import Source, StatementStaging, utc_now
from hisaab.parsing import templates
from tests.conftest import login, make_user
from tests.statement_samples import ruled_page, ruled_pdf
from tests.test_statement_spec import RULED

ROW: tuple[str, ...] = ("01/09/26", "CORNER SHOP", "", "100.00", "", "900.00")
SUMMARY: list[str] = ["STATEMENT SUMMARY", "Opening 1,000.00", "Closing 900.00"]
PASSING: bytes = ruled_pdf(ruled_page([ROW], first=True, summary=SUMMARY))
FAILING: bytes = ruled_pdf(ruled_page([ROW], first=True, summary=["Opening 1,000.00", "Closing 800.00"]))
PASSWORD: str = "pw-never-stored-7731"


@pytest.fixture
def asha(app: FastAPI, client: TestClient) -> TestClient:
    db: Database = app.state.db
    with db.write_session() as s:
        templates.save_statement_template(s, name="Example statement", spec=RULED, status="active")
    make_user(app, "asha", "asha password 1")
    login(client, "asha", "asha password 1")
    return client


def account(client: TestClient, last4: str, kind: str = "bank", name: str | None = None) -> str:
    created: dict[str, Any] = client.post("/api/accounts", json={
        "type": kind, "display_name": name or f"{kind} {last4}", "last4": last4}).json()
    return str(created["id"])


@pytest.fixture
def bank(asha: TestClient) -> str:
    return account(asha, "1234", name="Savings")


def upload(client: TestClient, data: bytes, password: str | None = None, filename: str = "sept.pdf") -> Response:
    return client.post("/api/statements", json={"filename": filename, "password": password,
                                                "content_base64": base64.b64encode(data).decode()})


def wait_job(client: TestClient, job_id: str) -> dict[str, Any]:
    deadline: float = time.monotonic() + 10
    while time.monotonic() < deadline:
        job: dict[str, Any] = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] not in ("queued", "running"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def counts(app: FastAPI) -> tuple[int, int]:
    db: Database = app.state.db
    with db.read_session() as s:
        return (s.scalar(select(func.count()).select_from(StatementStaging)) or 0,
                s.scalar(select(func.count()).select_from(Source)) or 0)


def test_requires_login(client: TestClient) -> None:
    assert client.get("/api/statements").status_code == 401
    assert upload(client, PASSING).status_code == 401


def test_upload_preview_and_merge(asha: TestClient, bank: str) -> None:
    resp: Response = upload(asha, PASSING)
    assert resp.status_code == 201, resp.text
    preview: dict[str, Any] = resp.json()
    assert (preview["filename"], preview["template_name"], preview["account_id"], preview["account_name"],
            preview["account_kind"]) == ("sept.pdf", "Example statement", bank, "Savings", "bank")
    assert [(r["date"], r["narration"], r["direction"], r["amount_paise"]) for r in preview["new"]] == [
        ("2026-09-01", "CORNER SHOP", "debit", 10000)]
    assert preview["matches"] == [] and preview["unparseable"] == []
    assert preview["diagnostic"]["outcome"] == "passed"
    assert asha.get(f"/api/statements/{preview['id']}").json() == preview
    assert [s["id"] for s in asha.get("/api/statements").json()] == [preview["id"]]

    job: dict[str, Any] = wait_job(asha, asha.post(f"/api/statements/{preview['id']}/merge", json={}).json()["job_id"])
    assert job["state"] == "succeeded", job
    assert job["result"] == {"new": 1, "matched": 0, "needs_review": 1, "unparseable": 0,  # payee not recognised
                             "balance_gaps": 0}  # the statement matches its own opening and closing
    assert asha.get(f"/api/statements/{preview['id']}").status_code == 404
    [listed] = asha.get("/api/transactions").json()
    assert (listed["provenance"], listed["amount_paise"]) == ("statement", 10000)


def test_a_second_upload_shows_the_rows_as_already_here_with_current_values(asha: TestClient, bank: str) -> None:
    first: dict[str, Any] = upload(asha, PASSING).json()
    wait_job(asha, asha.post(f"/api/statements/{first['id']}/merge", json={}).json()["job_id"])
    [txn] = asha.get("/api/transactions").json()
    asha.patch(f"/api/transactions/{txn['id']}", json={"amount_paise": 9900}).raise_for_status()
    again: dict[str, Any] = upload(asha, PASSING).json()
    assert again["new"] == []
    [match] = again["matches"]
    assert (match["transaction_id"], match["amount_paise"], match["current_amount_paise"], match["edited"]) == (
        txn["id"], 10000, 9900, ["amount"])


@pytest.mark.parametrize(("data", "password", "says"), [
    (b"%PDF- nonsense", None, "isn't a PDF"),
    (ruled_pdf(ruled_page([ROW], first=True), password=PASSWORD), None, "password-protected"),
    (ruled_pdf(ruled_page([ROW], first=True), password=PASSWORD), "wrong", "doesn't open"),
])
def test_a_refused_upload_is_422_and_writes_nothing(app: FastAPI, asha: TestClient, bank: str, data: bytes,
                                                    password: str | None, says: str) -> None:
    before: tuple[int, int] = counts(app)
    resp: Response = upload(asha, data, password)
    assert resp.status_code == 422 and says in resp.json()["detail"]
    assert counts(app) == before


def test_bad_base64_is_422(asha: TestClient) -> None:
    resp: Response = asha.post("/api/statements", json={"filename": "x.pdf", "password": None,
                                                        "content_base64": "not base64!!"})
    assert resp.status_code == 422 and resp.json()["detail"] == "That file couldn't be read."


def test_unbound_then_pick_the_account(asha: TestClient) -> None:
    preview: dict[str, Any] = upload(asha, PASSING).json()
    assert preview["account_id"] is None
    resp: Response = asha.post(f"/api/statements/{preview['id']}/merge", json={})
    assert resp.status_code == 409 and "Pick" in resp.json()["detail"]
    card: str = account(asha, "5555", kind="credit_card")
    assert asha.patch(f"/api/statements/{preview['id']}", json={"account_id": card}).status_code == 422
    savings: str = account(asha, "9999", name="Savings")
    picked: dict[str, Any] = asha.patch(f"/api/statements/{preview['id']}", json={"account_id": savings}).json()
    assert (picked["account_id"], picked["account_name"]) == (savings, "Savings")


def test_a_failed_check_needs_accept_gap(asha: TestClient, bank: str) -> None:
    preview: dict[str, Any] = upload(asha, FAILING).json()
    assert preview["diagnostic"]["outcome"] == "failed" and preview["diagnostic"]["gap_paise"] == -10000
    refused: Response = asha.post(f"/api/statements/{preview['id']}/merge", json={})
    assert refused.status_code == 409 and "don't add up" in refused.json()["detail"]
    job: dict[str, Any] = wait_job(asha, asha.post(f"/api/statements/{preview['id']}/merge",
                                                   json={"accept_gap": True}).json()["job_id"])
    assert job["state"] == "succeeded"


def test_discard(app: FastAPI, asha: TestClient, bank: str) -> None:
    preview: dict[str, Any] = upload(asha, PASSING).json()
    assert asha.delete(f"/api/statements/{preview['id']}").status_code == 204
    assert asha.get(f"/api/statements/{preview['id']}").status_code == 404
    assert counts(app) == (0, 0)


def test_an_expired_preview_is_gone(app: FastAPI, asha: TestClient, bank: str) -> None:
    preview: dict[str, Any] = upload(asha, PASSING).json()
    db: Database = app.state.db
    with db.write_session() as s:
        s.get_one(StatementStaging, preview["id"]).expires_at = utc_now(datetime.now(UTC) - timedelta(seconds=1))
    assert asha.get(f"/api/statements/{preview['id']}").status_code == 404
    assert asha.get("/api/statements").json() == []
    assert asha.post(f"/api/statements/{preview['id']}/merge", json={}).status_code == 404


def test_other_users_see_nothing(app: FastAPI, asha: TestClient, client: TestClient, bank: str) -> None:
    preview: dict[str, Any] = upload(asha, PASSING).json()
    make_user(app, "ravi", "ravi password 1")
    login(client, "ravi", "ravi password 1")
    assert client.get(f"/api/statements/{preview['id']}").status_code == 404
    assert client.get("/api/statements").json() == []
    assert client.post(f"/api/statements/{preview['id']}/merge", json={}).status_code == 404
    assert client.delete(f"/api/statements/{preview['id']}").status_code == 404


def test_the_password_is_never_stored(app: FastAPI, asha: TestClient, bank: str, settings: Settings) -> None:
    data: bytes = ruled_pdf(ruled_page([ROW], first=True, summary=SUMMARY), password=PASSWORD)
    preview: dict[str, Any] = upload(asha, data, PASSWORD).json()
    wait_job(asha, asha.post(f"/api/statements/{preview['id']}/merge", json={}).json()["job_id"])
    db_path: Path = Path(str(settings.database_path))
    for path in (db_path, db_path.with_name(db_path.name + "-wal")):
        if path.exists():
            assert PASSWORD.encode() not in path.read_bytes()


def test_an_oversize_file_says_so_and_a_long_password_is_never_echoed(asha: TestClient, bank: str) -> None:
    big: Response = asha.post("/api/statements", json={"filename": "x.pdf", "password": None,
                                                       "content_base64": "A" * (14_000_000 + 8)})
    assert big.status_code == 422 and big.json()["detail"] == "That file is over 10 MB."
    secret: str = "p" * 300
    long_pw: Response = upload(asha, PASSING, secret)
    assert long_pw.status_code == 422 and secret not in long_pw.text


def test_an_unknown_layout_is_read_by_the_model_and_merges_like_any_other(app: FastAPI, asha: TestClient,
                                                                          bank: str) -> None:
    from pydantic import SecretStr

    from tests.claude_fake import FakeClaude
    from tests.test_statement_reading import MODEL_ANSWER, UNKNOWN

    app.state.settings = app.state.settings.model_copy(update={"anthropic_api_key": SecretStr("sk-test")})
    claude: FakeClaude = FakeClaude(reply=lambda prompt: MODEL_ANSWER)
    app.state.statement_transport = claude
    preview: dict[str, Any] = upload(asha, UNKNOWN, filename="other.pdf").json()
    assert (preview["template_name"], preview["account_id"], preview["diagnostic"]["tier"]) == (None, bank, 3)
    assert [r["amount_paise"] for r in preview["new"]] == [10000] and claude.calls == 1
    job: dict[str, Any] = wait_job(asha, asha.post(f"/api/statements/{preview['id']}/merge", json={}).json()["job_id"])
    assert job["state"] == "succeeded" and job["result"]["new"] == 1


def test_an_unknown_layout_without_the_model_is_422_and_stages_nothing(app: FastAPI, asha: TestClient) -> None:
    from tests.test_statement_reading import UNKNOWN

    before: tuple[int, int] = counts(app)
    resp: Response = upload(asha, UNKNOWN)
    assert resp.status_code == 422 and "isn't available right now" in resp.json()["detail"]
    assert counts(app) == before
