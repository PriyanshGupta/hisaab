"""/api/transactions: manual entry, list, corrections (overlay), undo, delete."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import login, make_user

DAY = "2026-10-05"


@pytest.fixture
def asha(app, client):
    make_user(app, "asha", "asha password 1")
    login(client, "asha", "asha password 1")
    return client


@pytest.fixture
def wallet(asha):
    return asha.post("/api/accounts", json={"type": "cash", "display_name": "Wallet", "opening_balance_paise": 0,
                                            "opening_balance_date": "2026-07-01"}).json()["id"]


@pytest.fixture
def bank(asha):
    return asha.post("/api/accounts", json={"type": "bank", "display_name": "HDFC", "last4": "5512",
                                            "opening_balance_paise": 0, "opening_balance_date": "2026-07-01"}).json()["id"]


def enter(client, **kw):
    body = {"kind": "expense", "amount_paise": 40000, "transaction_date": DAY, "payee": "Tea stall"} | kw
    return client.post("/api/transactions", json=body)


def test_requires_login(client):
    assert client.get("/api/transactions").status_code == 401
    assert enter(client).status_code == 401


def test_manual_entry_round_trip(asha, wallet):
    resp = enter(asha, from_account_id=wallet)
    assert resp.status_code == 201
    t = resp.json()
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
def test_invalid_entries_are_refused(asha, wallet, body, status):
    resp = enter(asha, from_account_id=wallet, **body)
    assert resp.status_code == status
    assert asha.get("/api/transactions").json() == []


def test_the_overlay_keeps_the_source_value_through_repeated_edits(asha, wallet):
    t = enter(asha, from_account_id=wallet, amount_paise=40000).json()
    first = asha.patch(f"/api/transactions/{t['id']}", json={"amount_paise": 45000}).json()
    assert first["amount_paise"] == 45000
    assert first["edits"] == [{"field": "amount", "prior_value": "40000", "new_value": "45000"}]
    second = asha.patch(f"/api/transactions/{t['id']}", json={"amount_paise": 50000}).json()
    assert second["edits"] == [{"field": "amount", "prior_value": "40000", "new_value": "50000"}]
    undone = asha.post(f"/api/transactions/{t['id']}/revert", json={"fields": ["amount"]}).json()
    assert (undone["amount_paise"], undone["edits"]) == (40000, [])


def test_an_expense_becomes_a_transfer(asha, wallet, bank):
    t = enter(asha, from_account_id=bank, payee="ATM").json()
    resp = asha.patch(f"/api/transactions/{t['id']}", json={"kind": "transfer", "to_account_id": wallet})
    assert resp.status_code == 200
    assert (resp.json()["kind"], resp.json()["to_account_id"]) == ("transfer", wallet)


def test_an_invalid_correction_is_422_and_changes_nothing(asha, wallet):
    t = enter(asha, from_account_id=wallet).json()
    resp = asha.patch(f"/api/transactions/{t['id']}", json={"kind": "transfer"})
    assert resp.status_code == 422
    assert "two different accounts" in resp.json()["detail"]
    assert asha.get(f"/api/transactions/{t['id']}").json()["edits"] == []


def test_delete_a_manual_entry(asha, wallet):
    t = enter(asha, from_account_id=wallet).json()
    asha.patch(f"/api/transactions/{t['id']}", json={"amount_paise": 1})
    refused = asha.delete(f"/api/transactions/{t['id']}")
    assert refused.status_code == 409 and "amount" in refused.json()["detail"]
    assert asha.delete(f"/api/transactions/{t['id']}", params={"confirm": "true"}).status_code == 204
    assert asha.get("/api/transactions").json() == []


def test_another_users_transaction_is_404_everywhere(app, asha, wallet):
    t = enter(asha, from_account_id=wallet).json()
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
