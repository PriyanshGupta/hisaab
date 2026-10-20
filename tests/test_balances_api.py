"""/api/balances and checkpoints (tech-design §8.3, §8.4): per account, never summed; open → investigated | accepted."""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

from tests.conftest import login, make_user

DAY: str = "2026-08-20"


@pytest.fixture
def asha(app: FastAPI, client: TestClient) -> TestClient:
    make_user(app, "asha", "asha password 1")
    login(client, "asha", "asha password 1")
    return client


def account(client: TestClient, name: str, kind: str = "bank", opening: int | None = 100000) -> str:
    body: dict[str, Any] = {"type": kind, "display_name": name}
    if kind != "cash":
        body["last4"] = str(1000 + sum(map(ord, name)) % 9000)  # banks and cards need their last 4
    if opening is not None:
        body |= {"opening_balance_paise": opening, "opening_balance_date": "2026-07-01"}
    resp: Response = client.post("/api/accounts", json=body)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def balances(client: TestClient) -> dict[str, dict[str, Any]]:
    return {b["name"]: b for b in client.get("/api/balances").json()}


def checkpoint(client: TestClient, acct: str, paise: int, on: str = DAY) -> Response:
    return client.post(f"/api/accounts/{acct}/checkpoints", json={"balance_paise": paise, "on": on})


def test_requires_login(client: TestClient) -> None:
    assert client.get("/api/balances").status_code == 401


def test_the_figure_is_the_latest_anchor_plus_movements_since(asha: TestClient) -> None:
    bank: str = account(asha, "Savings")
    asha.post("/api/transactions", json={"kind": "expense", "amount_paise": 20000, "transaction_date": DAY,
                                         "payee": "Shop", "from_account_id": bank}).raise_for_status()
    b: dict[str, Any] = balances(asha)["Savings"]
    assert (b["balance_paise"], b["transactions_since"], b["gaps"]) == (80000, 1, [])
    assert b["verified"] == {"balance_paise": 100000, "on": "2026-07-01", "source": "opening"}


def test_an_account_with_nothing_to_start_from_says_so(asha: TestClient) -> None:
    account(asha, "Wallet", "cash", opening=None)
    b: dict[str, Any] = balances(asha)["Wallet"]
    assert (b["verified"], b["balance_paise"], b["gaps"]) == (None, None, [])


def test_checkpoint_gap_accept_round_trip(asha: TestClient) -> None:
    bank: str = account(asha, "Savings")
    resp: Response = checkpoint(asha, bank, 90000)
    assert resp.status_code == 201
    [gap] = resp.json()["gaps"]
    assert (gap["observed_paise"], gap["expected_paise"], gap["gap_paise"], gap["since"], gap["since_source"],
            gap["observed_source"], gap["state"]) == (90000, 100000, -10000, "2026-07-01", "opening", "manual", "open")
    accepted: dict[str, Any] = asha.post(f"/api/balances/checks/{gap['observation_id']}/accept").json()
    assert accepted["gaps"] == [] and accepted["balance_paise"] == 90000
    [adj] = [t for t in asha.get("/api/transactions").json() if t["kind"] == "adjustment"]
    assert (adj["amount_paise"], adj["from_account_id"], adj["transaction_date"], adj["provenance"]) == (
        10000, bank, DAY, "manual")


def test_fix_then_close_as_investigated(asha: TestClient) -> None:
    bank: str = account(asha, "Savings")
    spent: dict[str, Any] = asha.post("/api/transactions", json={
        "kind": "expense", "amount_paise": 10000, "transaction_date": "2026-08-05", "payee": "Shop",
        "from_account_id": bank}).json()
    [gap] = checkpoint(asha, bank, 100000).json()["gaps"]  # the bank says nothing was spent
    asha.patch(f"/api/transactions/{spent['id']}", json={"amount_paise": 5000}).raise_for_status()  # half fixed: ₹50 left
    assert balances(asha)["Savings"]["gaps"][0]["state"] == "open"
    refused: Response = asha.post(f"/api/balances/checks/{gap['observation_id']}/close")
    assert refused.status_code == 422 and "still there" in refused.json()["detail"]
    asha.delete(f"/api/transactions/{spent['id']}", params={"confirm": True}).raise_for_status()  # the real fix
    fixed: dict[str, Any] = balances(asha)["Savings"]
    assert [(g["state"], g["gap_paise"]) for g in fixed["gaps"]] == [("fixed", 0)]
    closed: dict[str, Any] = asha.post(f"/api/balances/checks/{gap['observation_id']}/close").json()
    assert closed["gaps"] == []
    assert not [t for t in asha.get("/api/transactions").json() if t["kind"] == "adjustment"]


def test_refusals_are_sentences(asha: TestClient) -> None:
    card: str = account(asha, "Card", "credit_card")
    resp: Response = checkpoint(asha, card, -5000)
    assert resp.status_code == 422 and "positive amount" in resp.json()["detail"]
    assert checkpoint(asha, card, 100000, on="2999-01-01").status_code == 422
    [gap] = checkpoint(asha, card, 90000).json()["gaps"]
    asha.post(f"/api/balances/checks/{gap['observation_id']}/accept").raise_for_status()
    twice: Response = asha.post(f"/api/balances/checks/{gap['observation_id']}/accept")
    assert twice.status_code == 422 and "already matches" in twice.json()["detail"]
    assert asha.post("/api/accounts/no-such/checkpoints", json={"balance_paise": 1, "on": DAY}).status_code == 404


def test_gaps_are_per_account_and_never_summed(asha: TestClient) -> None:
    bank: str = account(asha, "Savings")
    card: str = account(asha, "Card", "credit_card")
    checkpoint(asha, bank, 90000)
    checkpoint(asha, card, 130000)
    found: dict[str, dict[str, Any]] = balances(asha)
    assert [g["gap_paise"] for g in found["Savings"]["gaps"]] == [-10000]
    assert [g["gap_paise"] for g in found["Card"]["gaps"]] == [30000]
    payload: str = str(asha.get("/api/balances").json())
    assert "total" not in payload.lower()


def test_other_users_see_nothing(app: FastAPI, asha: TestClient, client: TestClient) -> None:
    bank: str = account(asha, "Savings")
    [gap] = checkpoint(asha, bank, 90000).json()["gaps"]
    make_user(app, "ravi", "ravi password 1")
    login(client, "ravi", "ravi password 1")
    assert client.get("/api/balances").json() == []
    assert client.post(f"/api/balances/checks/{gap['observation_id']}/accept").status_code == 404
    assert checkpoint(client, bank, 1).status_code == 404


def test_an_edit_that_breaks_a_card_balance_opens_a_gap_that_closes_after_the_fix(asha: TestClient) -> None:
    """Live check 1 and 2 through the API alone: nobody calls record_checks by hand."""
    card: str = account(asha, "Card", "credit_card", opening=300000)
    bank: str = account(asha, "Savings", opening=1000000)
    bill: dict[str, Any] = asha.post("/api/transactions", json={
        "kind": "transfer", "amount_paise": 300000, "transaction_date": "2026-08-05",
        "from_account_id": bank, "to_account_id": card}).json()
    assert checkpoint(asha, card, 0).json()["gaps"] == []  # the bill paid it off
    asha.patch(f"/api/transactions/{bill['id']}", json={"kind": "expense", "to_account_id": None}).raise_for_status()
    [gap] = balances(asha)["Card"]["gaps"]
    assert (gap["gap_paise"], gap["state"]) == (-300000, "open")
    asha.post(f"/api/transactions/{bill['id']}/revert", json={"fields": ["kind", "to_account_id"]}).raise_for_status()
    [fixed] = balances(asha)["Card"]["gaps"]
    assert (fixed["state"], fixed["gap_paise"]) == ("fixed", 0)
    assert asha.post(f"/api/balances/checks/{gap['observation_id']}/close").json()["gaps"] == []


def test_deleting_an_accepted_adjustment_reopens_the_gap_instead_of_failing(asha: TestClient) -> None:
    bank: str = account(asha, "Savings")
    [gap] = checkpoint(asha, bank, 90000).json()["gaps"]
    asha.post(f"/api/balances/checks/{gap['observation_id']}/accept").raise_for_status()
    [adj] = [t for t in asha.get("/api/transactions").json() if t["kind"] == "adjustment"]
    assert asha.delete(f"/api/transactions/{adj['id']}").status_code == 204
    assert [(g["state"], g["gap_paise"]) for g in balances(asha)["Savings"]["gaps"]] == [("open", -10000)]
