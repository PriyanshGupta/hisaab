"""/api/accounts: the HTTP surface of accounts and identifiers."""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

from tests.conftest import login, make_user

ANCHOR: str = "2026-07-11"
BANK: dict[str, Any] = {"type": "bank", "display_name": "HDFC Savings", "institution": "HDFC Bank", "last4": "5512",
        "opening_balance_paise": 8420000, "opening_balance_date": ANCHOR}
CARD: dict[str, Any] = {"type": "credit_card", "display_name": "Regalia card", "institution": "HDFC Bank", "last4": "8830",
        "opening_balance_paise": 4700000, "opening_balance_date": ANCHOR}
CASH: dict[str, Any] = {"type": "cash", "display_name": "Wallet", "opening_balance_paise": 350000, "opening_balance_date": ANCHOR}


@pytest.fixture
def asha(app: FastAPI, client: TestClient) -> TestClient:
    make_user(app, "asha", "asha password 1")
    login(client, "asha", "asha password 1")
    return client


def test_requires_login(client: TestClient) -> None:
    assert client.get("/api/accounts").status_code == 401
    assert client.post("/api/accounts", json=BANK).status_code == 401


def test_create_all_three_types_and_list_them(asha: TestClient) -> None:
    for body in (BANK, CARD, CASH):
        assert asha.post("/api/accounts", json=body).status_code == 201
    listed: Any = asha.get("/api/accounts").json()
    assert [(a["type"], a["last4"], a["opening_balance_paise"]) for a in listed] == [
        ("bank", "5512", 8420000), ("credit_card", "8830", 4700000), ("cash", None, 350000),
    ]
    card: Any = listed[1]
    assert card["identifiers"] == [{"id": card["identifiers"][0]["id"], "kind": "card_last4",
                                    "identifier": "8830", "removable": False}]


def test_duplicate_last4_is_409_with_a_clear_message(asha: TestClient) -> None:
    asha.post("/api/accounts", json=BANK)
    resp: Response = asha.post("/api/accounts", json=CARD | {"last4": "5512"})
    assert resp.status_code == 409
    assert resp.json() == {"detail": "You already have an account ending 5512: HDFC Savings."}


@pytest.mark.parametrize("change,status", [
    ({"opening_balance_paise": -4700000}, 422),   # card debt must be positive
    ({"opening_balance_paise": 4700000.5}, 422),  # paise are integers, never floats
    ({"opening_balance_paise": "4700000"}, 422),
    ({"last4": None}, 422),
    ({"type": "wallet"}, 422),
    ({"opening_balance_date": "11/07/2026"}, 422),
])
def test_invalid_card_is_refused(asha: TestClient, change: dict[str, Any], status: int) -> None:
    resp: Response = asha.post("/api/accounts", json=CARD | change)
    assert resp.status_code == status
    assert asha.get("/api/accounts").json() == []


def test_negative_card_message_explains_the_sign(asha: TestClient) -> None:
    resp: Response = asha.post("/api/accounts", json=CARD | {"opening_balance_paise": -4700000})
    assert "amount owed" in resp.json()["detail"]


def test_edit_renames_and_corrects_the_anchor(asha: TestClient) -> None:
    acct: Any = asha.post("/api/accounts", json=BANK).json()
    resp: Response = asha.patch(f"/api/accounts/{acct['id']}", json={"display_name": "HDFC Salary",
                                                            "opening_balance_paise": 9000000})
    assert resp.status_code == 200
    assert (resp.json()["display_name"], resp.json()["opening_balance_paise"], resp.json()["institution"]) == (
        "HDFC Salary", 9000000, "HDFC Bank")  # unspecified fields unchanged


# --- the opening balance is optional (ruling 2026-10-12) ----------------------------------

def test_an_account_can_be_added_without_an_opening_balance(asha: TestClient) -> None:
    body: dict[str, Any] = {k: v for k, v in CASH.items() if not k.startswith("opening")}
    resp: Response = asha.post("/api/accounts", json=body)
    assert resp.status_code == 201, resp.text
    assert (resp.json()["opening_balance_paise"], resp.json()["opening_balance_date"]) == (None, None)


@pytest.mark.parametrize("half", [{"opening_balance_paise": 100}, {"opening_balance_date": ANCHOR}])
def test_a_balance_needs_its_date_and_a_date_its_balance(asha: TestClient, half: dict[str, Any]) -> None:
    body: dict[str, Any] = {k: v for k, v in CASH.items() if not k.startswith("opening")} | half
    resp: Response = asha.post("/api/accounts", json=body)
    assert resp.status_code == 422 and resp.json()["detail"] == "Enter both the balance and its date, or neither."


def test_an_anchor_can_be_added_later_and_removed(asha: TestClient) -> None:
    created: str = asha.post("/api/accounts", json={"type": "cash", "display_name": "Wallet"}).json()["id"]
    added: Response = asha.patch(f"/api/accounts/{created}",
                                 json={"opening_balance_paise": 5000, "opening_balance_date": ANCHOR})
    assert added.json()["opening_balance_paise"] == 5000
    cleared: Response = asha.patch(f"/api/accounts/{created}",
                                   json={"opening_balance_paise": None, "opening_balance_date": None})
    assert (cleared.json()["opening_balance_paise"], cleared.json()["opening_balance_date"]) == (None, None)


def test_identifier_lookup_is_case_insensitive(asha: TestClient) -> None:
    acct: Any = asha.post("/api/accounts", json=BANK).json()
    resp: Response = asha.post(f"/api/accounts/{acct['id']}/identifiers", json={"kind": "vpa", "identifier": "upi@ybl"})
    assert resp.status_code == 201 and resp.json()["identifier"] == "upi@ybl"
    found: Response = asha.get("/api/accounts/resolve", params={"identifier": "UPI@YBL"})
    assert found.status_code == 200 and found.json()["id"] == acct["id"]


def test_resolving_an_unknown_identifier_is_404(asha: TestClient) -> None:
    assert asha.get("/api/accounts/resolve", params={"identifier": "nobody@ybl"}).status_code == 404


def test_archive_hides_the_account_and_stops_resolution(asha: TestClient) -> None:
    acct: Any = asha.post("/api/accounts", json=BANK).json()
    asha.post(f"/api/accounts/{acct['id']}/identifiers", json={"kind": "vpa", "identifier": "upi@ybl"})
    resp: Response = asha.post(f"/api/accounts/{acct['id']}/archive")
    assert resp.status_code == 200 and resp.json()["archived_at"] is not None
    assert asha.get("/api/accounts").json() == []
    archived: Any = asha.get("/api/accounts", params={"include_archived": "true"}).json()
    assert [a["id"] for a in archived] == [acct["id"]] and archived[0]["identifiers"] == []
    assert asha.get("/api/accounts/resolve", params={"identifier": "UPI@YBL"}).status_code == 404
    assert asha.get("/api/accounts/resolve", params={"identifier": "5512"}).status_code == 404


def test_archived_account_cannot_be_edited(asha: TestClient) -> None:
    acct: Any = asha.post("/api/accounts", json=BANK).json()
    asha.post(f"/api/accounts/{acct['id']}/archive")
    assert asha.patch(f"/api/accounts/{acct['id']}", json={"display_name": "x"}).status_code == 422


def test_remove_a_vpa_but_not_the_last4(asha: TestClient) -> None:
    acct: Any = asha.post("/api/accounts", json=BANK).json()
    vpa: Any = asha.post(f"/api/accounts/{acct['id']}/identifiers", json={"kind": "vpa", "identifier": "a@ybl"}).json()
    setup: Any = acct["identifiers"][0]
    assert asha.delete(f"/api/accounts/{acct['id']}/identifiers/{setup['id']}").status_code == 422
    assert asha.delete(f"/api/accounts/{acct['id']}/identifiers/{vpa['id']}").status_code == 204
    assert asha.get("/api/accounts/resolve", params={"identifier": "a@ybl"}).status_code == 404


def test_duplicate_vpa_is_409(asha: TestClient) -> None:
    a: Any = asha.post("/api/accounts", json=BANK).json()
    b: Any = asha.post("/api/accounts", json=CARD).json()
    asha.post(f"/api/accounts/{a['id']}/identifiers", json={"kind": "vpa", "identifier": "a@ybl"})
    resp: Response = asha.post(f"/api/accounts/{b['id']}/identifiers", json={"kind": "vpa", "identifier": "A@YBL"})
    assert resp.status_code == 409 and resp.json()["detail"] == "a@ybl is already on HDFC Savings."


def test_another_users_account_is_404_everywhere(app: FastAPI, asha: TestClient) -> None:
    acct: Any = asha.post("/api/accounts", json=BANK).json()
    asha.post(f"/api/accounts/{acct['id']}/identifiers", json={"kind": "vpa", "identifier": "asha@ybl"})
    make_user(app, "ravi", "ravi password 1")
    with TestClient(app) as ravi:
        login(ravi, "ravi", "ravi password 1")
        assert ravi.get("/api/accounts", params={"include_archived": "true"}).json() == []
        assert ravi.get(f"/api/accounts/{acct['id']}").status_code == 404
        assert ravi.patch(f"/api/accounts/{acct['id']}", json={"display_name": "mine"}).status_code == 404
        assert ravi.post(f"/api/accounts/{acct['id']}/archive").status_code == 404
        assert ravi.post(f"/api/accounts/{acct['id']}/identifiers",
                         json={"kind": "vpa", "identifier": "r@ybl"}).status_code == 404
        assert ravi.get("/api/accounts/resolve", params={"identifier": "asha@ybl"}).status_code == 404
    assert asha.get(f"/api/accounts/{acct['id']}").json()["display_name"] == "HDFC Savings"


# --- changing the type: a card entered as a bank by mistake (user, 2026-10-19) ---------------------

def _me(client: TestClient) -> str:
    me: str = client.get("/api/auth/me").json()["id"]
    return me


def _kinds(app: FastAPI, user_id: str) -> list[tuple[str, int]]:
    from sqlalchemy import select

    from hisaab.db import Database
    from hisaab.models import Transaction

    db: Database = app.state.db
    with db.read_session() as s:
        return sorted((t.kind, t.amount) for t in s.scalars(select(Transaction).where(Transaction.user_id == user_id)))


def test_a_bank_that_is_really_a_card_is_switched_and_its_credits_are_read_again(app: FastAPI, asha: TestClient) -> None:
    from tests.pipeline_helpers import CARD_EMAIL, contract, ingest

    account: Any = asha.post("/api/accounts", json=BANK | {"last4": "6203"}).json()
    ingest(app.state.db, _me(asha), (contract(direction="credit", account_identifier="6203", payee_raw=None,
                                              amount=2468000), CARD_EMAIL),
           (contract(direction="credit", account_identifier="6203", payee_raw="CASHBACK", amount=5000), CARD_EMAIL))
    assert _kinds(app, _me(asha)) == [("income", 5000), ("income", 2468000)]
    resp: Response = asha.patch(f"/api/accounts/{account['id']}", json={"type": "credit_card"})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["type"], resp.json()["identifiers"][0]["kind"]) == ("credit_card", "card_last4")
    assert _kinds(app, _me(asha)) == [("refund", 5000), ("transfer", 2468000)]  # cashback; a bill paid, payer unknown
    report: Any = asha.get("/api/report", params={"start": "2026-09-01", "end": "2026-09-30"}).json()
    assert report["money_in_paise"] == 0
    back: Response = asha.patch(f"/api/accounts/{account['id']}", json={"type": "bank"})
    assert back.status_code == 200 and _kinds(app, _me(asha)) == [("income", 5000), ("income", 2468000)]


@pytest.mark.parametrize("setup,body,message", [
    ("manual_income", {"type": "credit_card"}, "income"),
    ("upi", {"type": "credit_card"}, "UPI"),
    ("overdraft", {"type": "credit_card"}, "owed"),
    ("cash", {"type": "bank"}, "cash"),
])
def test_a_type_change_that_cannot_fit_is_refused_and_nothing_changes(asha: TestClient, setup: str,
                                                                     body: dict[str, Any], message: str) -> None:
    account: Any = asha.post("/api/accounts", json=CASH if setup == "cash" else
                             BANK | ({"opening_balance_paise": -50000} if setup == "overdraft" else {})).json()
    if setup == "manual_income":
        asha.post("/api/transactions", json={"kind": "income", "amount_paise": 100000, "transaction_date": "2026-09-01",
                                             "payee": "Employer", "to_account_id": account["id"]})
    if setup == "upi":
        asha.post(f"/api/accounts/{account['id']}/identifiers", json={"kind": "vpa", "identifier": "asha@okexample"})
    resp: Response = asha.patch(f"/api/accounts/{account['id']}", json=body)
    assert resp.status_code == 422 and message in resp.json()["detail"], resp.text
    assert asha.get(f"/api/accounts/{account['id']}").json()["type"] == account["type"]
