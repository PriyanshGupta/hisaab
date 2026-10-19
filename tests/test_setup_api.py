"""/api/setup (slice 16; tech-design §5.4): first-run order, the backfill's state, the categorize groups."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Connection

from hisaab.ingest.sync import backfill_start
from tests import factories as f
from tests.conftest import login, make_user
from tests.test_gmail_api import connect, fake, gmail_app  # noqa: F401  (fixtures)

SYNCED: str = "2026-10-11T00:00:00.000Z"


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        make_user(app, "asha", "asha password 1")
        login(c, "asha", "asha password 1")
        yield c


def me(client: TestClient) -> str:
    uid: str = client.get("/api/auth/me").json()["id"]
    return uid


def write(app: FastAPI) -> Any:
    return app.state.db.write_session()


def start_of(app: FastAPI, user_id: str) -> str:
    with app.state.db.read_session() as s:
        return backfill_start(s, user_id).isoformat()


def step(client: TestClient) -> str:
    value: str = client.get("/api/setup").json()["step"]
    return value


# --- the step -----------------------------------------------------------------------------------

def test_a_new_user_starts_at_accounts_with_the_backfill_start(client: TestClient, app: FastAPI) -> None:
    body: dict[str, Any] = client.get("/api/setup").json()
    assert body == {"step": "accounts", "backfill_start": start_of(app, me(client)), "job": None}


def test_with_an_account_the_next_step_is_gmail(client: TestClient, app: FastAPI) -> None:
    with write(app) as s:
        f.account(s.connection(), me(client))
    assert step(client) == "gmail"


def test_an_archived_account_does_not_count(client: TestClient, app: FastAPI) -> None:
    with write(app) as s:
        f.account(s.connection(), me(client), archived_at=SYNCED)
    assert step(client) == "accounts"


def test_connected_and_not_yet_synced_is_backfill_with_its_job(client: TestClient, app: FastAPI) -> None:
    uid: str = me(client)
    with write(app) as s:
        f.account(s.connection(), uid)
        f.gmail_connection(s.connection(), user_id=uid)
        f.job(s.connection(), id="old", user_id=uid, kind="backfill", state="failed", created_at="2026-10-10T00:00:00.000Z",
              error='{"message": "interrupted by shutdown"}')
        f.job(s.connection(), id="now", user_id=uid, kind="backfill", state="running", created_at=SYNCED,
              progress='{"fetched": 100, "estimate": 412}')
    body: dict[str, Any] = client.get("/api/setup").json()
    assert body["step"] == "backfill"
    assert (body["job"]["id"], body["job"]["progress"]) == ("now", {"fetched": 100, "estimate": 412})


def test_a_needs_reauth_connection_mid_backfill_is_back_at_gmail(client: TestClient, app: FastAPI) -> None:
    uid: str = me(client)
    with write(app) as s:
        f.account(s.connection(), uid)
        f.gmail_connection(s.connection(), user_id=uid, status="needs_reauth")
    assert step(client) == "gmail"


def finish_backfill(app: FastAPI, uid: str) -> None:
    with write(app) as s:
        f.account(s.connection(), uid)
        f.gmail_connection(s.connection(), user_id=uid, last_successful_sync_at=SYNCED)


def test_after_the_backfill_with_nothing_categorized_is_categorize(client: TestClient, app: FastAPI) -> None:
    uid: str = me(client)
    finish_backfill(app, uid)
    with write(app) as s:
        f.transaction(s.connection(), uid)
    assert step(client) == "categorize"


def test_once_anything_is_categorized_setup_is_done(client: TestClient, app: FastAPI) -> None:
    uid: str = me(client)
    finish_backfill(app, uid)
    with write(app) as s:
        c: Connection = s.connection()
        f.transaction(c, uid)
        edited: str = f.transaction(c, uid)["id"]
        f.transaction_edit(c, edited, uid, field="category_override_id", new_value=f.category(c, uid)["id"])
    assert step(client) == "done"


def test_a_backfill_that_found_no_transactions_is_done(client: TestClient, app: FastAPI) -> None:
    finish_backfill(app, me(client))
    assert step(client) == "done"  # the (empty) report, not a categorize screen with nothing on it


# --- the order is enforced: no Gmail before accounts and their balances --------------------------

@pytest.fixture
def asha(gmail_app: FastAPI) -> Iterator[TestClient]:  # noqa: F811
    with TestClient(gmail_app) as c:
        make_user(gmail_app, "asha", "asha password 1")
        login(c, "asha", "asha password 1")
        yield c


def add_account(client: TestClient, name: str, date: str) -> None:
    resp: Response = client.post("/api/accounts", json={
        "type": "bank", "display_name": name, "last4": "1111", "opening_balance_paise": 0,
        "opening_balance_date": date})
    assert resp.status_code == 201, resp.text


def test_connect_before_any_account_is_refused(asha: TestClient) -> None:
    resp: Response = asha.post("/api/gmail/connect")
    assert resp.status_code == 409 and resp.json()["detail"] == "Add your accounts first."


def test_connect_with_a_balance_dated_after_the_start_names_the_account(asha: TestClient, gmail_app: FastAPI) -> None:
    add_account(asha, "HDFC Savings", "2026-10-01")
    resp: Response = asha.post("/api/gmail/connect")
    assert resp.status_code == 409
    detail: str = resp.json()["detail"]
    assert "HDFC Savings is dated 1 Oct 2026" in detail and "enter 0" in detail


def test_connect_with_balances_on_the_start_goes_ahead(asha: TestClient, gmail_app: FastAPI) -> None:
    add_account(asha, "HDFC Savings", start_of(gmail_app, me(asha)))
    assert asha.post("/api/gmail/connect").status_code == 200


def test_an_account_without_an_opening_balance_does_not_hold_up_connecting(asha: TestClient) -> None:
    assert asha.post("/api/accounts", json={"type": "cash", "display_name": "Wallet"}).status_code == 201
    assert asha.post("/api/gmail/connect").status_code == 200


def test_a_reconnect_after_the_first_sync_is_not_gated(asha: TestClient, gmail_app: FastAPI) -> None:
    with write(gmail_app) as s:
        f.gmail_connection(s.connection(), user_id=me(asha), status="needs_reauth", last_successful_sync_at=SYNCED)
    assert asha.post("/api/gmail/connect").status_code == 200  # no accounts, yet not first run


def test_the_callback_returns_to_setup_during_first_run(asha: TestClient, gmail_app: FastAPI) -> None:
    add_account(asha, "HDFC Savings", start_of(gmail_app, me(asha)))
    assert connect(asha).headers["location"] == "/setup?gmail=connected"
