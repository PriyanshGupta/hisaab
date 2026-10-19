"""/api/transactions: manual entry, list, corrections (overlay), undo, delete."""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

from sqlalchemy import Connection

from tests import factories as f
from tests.conftest import login, make_user

DAY: str = "2026-10-05"


@pytest.fixture
def asha(app: FastAPI, client: TestClient) -> TestClient:
    make_user(app, "asha", "asha password 1")
    login(client, "asha", "asha password 1")
    return client


@pytest.fixture
def wallet(asha: TestClient) -> str:
    created: dict[str, Any] = asha.post("/api/accounts", json={"type": "cash", "display_name": "Wallet", "opening_balance_paise": 0,
                                            "opening_balance_date": "2026-07-01"}).json()
    return str(created["id"])


@pytest.fixture
def bank(asha: TestClient) -> str:
    created: dict[str, Any] = asha.post("/api/accounts", json={"type": "bank", "display_name": "HDFC", "last4": "5512",
                                            "opening_balance_paise": 0, "opening_balance_date": "2026-07-01"}).json()
    return str(created["id"])


def enter(client: TestClient, **kw: Any) -> Response:
    body: dict[str, Any] = {"kind": "expense", "amount_paise": 40000, "transaction_date": DAY, "payee": "Tea stall"} | kw
    return client.post("/api/transactions", json=body)


def test_requires_login(client: TestClient) -> None:
    assert client.get("/api/transactions").status_code == 401
    assert enter(client).status_code == 401


def test_manual_entry_round_trip(asha: TestClient, wallet: str) -> None:
    resp: Response = enter(asha, from_account_id=wallet)
    assert resp.status_code == 201
    t: Any = resp.json()
    assert {k: t[k] for k in ("kind", "amount_paise", "from_account_id", "to_account_id", "payee_raw",
                              "transaction_date", "status", "provenance", "edits")} == {
        "kind": "expense", "amount_paise": 40000, "from_account_id": wallet, "to_account_id": None,
        "payee_raw": "Tea stall", "transaction_date": DAY, "status": "confirmed", "provenance": "manual", "edits": [],
    }
    assert [x["id"] for x in asha.get("/api/transactions").json()] == [t["id"]]


@pytest.mark.parametrize("body,status", [
    ({"kind": "income"}, 422),                       # income can't come from the wallet
    ({"amount_paise": 400.5}, 422),                 # paise are integers
    ({"amount_paise": 0}, 422),
    ({"kind": "lending"}, 422),
    ({"transaction_date": "2099-01-01"}, 422),
])
def test_invalid_entries_are_refused(asha: TestClient, wallet: str, body: dict[str, Any], status: int) -> None:
    resp: Response = enter(asha, from_account_id=wallet, **body)
    assert resp.status_code == status
    assert asha.get("/api/transactions").json() == []


def test_the_overlay_keeps_the_source_value_through_repeated_edits(asha: TestClient, wallet: str) -> None:
    t: Any = enter(asha, from_account_id=wallet, amount_paise=40000).json()
    first: Any = asha.patch(f"/api/transactions/{t['id']}", json={"amount_paise": 45000}).json()
    assert first["amount_paise"] == 45000
    assert first["edits"] == [{"field": "amount", "prior_value": "40000", "new_value": "45000"}]
    second: Any = asha.patch(f"/api/transactions/{t['id']}", json={"amount_paise": 50000}).json()
    assert second["edits"] == [{"field": "amount", "prior_value": "40000", "new_value": "50000"}]
    undone: Any = asha.post(f"/api/transactions/{t['id']}/revert", json={"fields": ["amount"]}).json()
    assert (undone["amount_paise"], undone["edits"]) == (40000, [])


def test_an_expense_becomes_a_transfer(asha: TestClient, wallet: str, bank: str) -> None:
    t: Any = enter(asha, from_account_id=bank, payee="ATM").json()
    resp: Response = asha.patch(f"/api/transactions/{t['id']}", json={"kind": "transfer", "to_account_id": wallet})
    assert resp.status_code == 200
    assert (resp.json()["kind"], resp.json()["to_account_id"]) == ("transfer", wallet)


def test_an_invalid_correction_is_422_and_changes_nothing(asha: TestClient, wallet: str) -> None:
    t: Any = enter(asha, from_account_id=wallet).json()
    resp: Response = asha.patch(f"/api/transactions/{t['id']}", json={"kind": "transfer"})
    assert resp.status_code == 422
    assert "two different accounts" in resp.json()["detail"]
    assert asha.get(f"/api/transactions/{t['id']}").json()["edits"] == []


def test_delete_a_manual_entry(asha: TestClient, wallet: str) -> None:
    t: Any = enter(asha, from_account_id=wallet).json()
    asha.patch(f"/api/transactions/{t['id']}", json={"amount_paise": 1})
    refused: Response = asha.delete(f"/api/transactions/{t['id']}")
    assert refused.status_code == 409 and "amount" in refused.json()["detail"]
    assert asha.delete(f"/api/transactions/{t['id']}", params={"confirm": "true"}).status_code == 204
    assert asha.get("/api/transactions").json() == []


def test_another_users_transaction_is_404_everywhere(app: FastAPI, asha: TestClient, wallet: str) -> None:
    t: Any = enter(asha, from_account_id=wallet).json()
    make_user(app, "ravi", "ravi password 1")
    with TestClient(app) as ravi:
        login(ravi, "ravi", "ravi password 1")
        assert ravi.get("/api/transactions").json() == []
        assert ravi.get(f"/api/transactions/{t['id']}").status_code == 404
        assert ravi.patch(f"/api/transactions/{t['id']}", json={"amount_paise": 1}).status_code == 404
        assert ravi.post(f"/api/transactions/{t['id']}/revert", json={"fields": ["amount"]}).status_code == 404
        assert ravi.delete(f"/api/transactions/{t['id']}", params={"confirm": "true"}).status_code == 404
        assert enter(ravi, from_account_id=wallet).status_code == 422  # asha's wallet is not ravi's
    assert asha.get(f"/api/transactions/{t['id']}").json()["amount_paise"] == 40000


def test_looks_right_confirms_a_flagged_transaction(app: FastAPI, asha: TestClient) -> None:
    uid: str = asha.get("/api/auth/me").json()["id"]
    with app.state.db.write_session() as s:
        c: Connection = s.connection()
        txn: str = f.transaction(c, uid, amount=3_000_000, status="needs_review", review_reason="amount over ₹25,000",
                                 payee_id=f.payee(c)["id"], from_account_id=f.account(c, uid)["id"])["id"]
    resp: Response = asha.post(f"/api/transactions/{txn}/acknowledge")
    assert resp.status_code == 200 and resp.json()["status"] == "confirmed"


def test_a_manual_entry_is_never_flagged_by_its_own_correction(asha: TestClient, wallet: str) -> None:
    created: str = enter(asha, from_account_id=wallet).json()["id"]
    resp: Response = asha.patch(f"/api/transactions/{created}", json={"amount_paise": 3_000_000})
    assert resp.json()["status"] == "confirmed"  # the user is the source: no guardrail questions them
