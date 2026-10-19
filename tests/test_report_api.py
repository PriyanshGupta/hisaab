"""GET /api/report (slice 18; tech-design §8, PRD §7): spending by category, both totals, transfers.

Built from manual entries through the API, so every number here traces to a line in the test.
"""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import update

from hisaab.db import Database
from hisaab.models import Category, Transaction
from tests.conftest import login, make_user

OCT: dict[str, str] = {"start": "2026-10-01", "end": "2026-10-31"}


@pytest.fixture
def asha(app: FastAPI, client: TestClient) -> TestClient:
    make_user(app, "asha", "asha password 1")
    login(client, "asha", "asha password 1")
    return client


def account(client: TestClient, type_: str, name: str, last4: str | None = None) -> str:
    body: dict[str, Any] = {"type": type_, "display_name": name, "opening_balance_paise": 0,
                            "opening_balance_date": "2026-07-01"}
    if last4:
        body["last4"] = last4
    created: dict[str, Any] = client.post("/api/accounts", json=body).json()
    return str(created["id"])


@pytest.fixture
def accts(asha: TestClient) -> dict[str, str]:
    return {"bank": account(asha, "bank", "Bank", "1111"), "card": account(asha, "credit_card", "Card", "2222"),
            "cash": account(asha, "cash", "Wallet")}


def category(client: TestClient, name: str) -> str:
    created: dict[str, Any] = client.post("/api/categories", json={"name": name}).json()
    return str(created["id"])


def enter(client: TestClient, **kw: Any) -> str:
    body: dict[str, Any] = {"kind": "expense", "amount_paise": 10000, "transaction_date": "2026-10-05"} | kw
    resp: Response = client.post("/api/transactions", json=body)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def get(client: TestClient, **period: str) -> dict[str, Any]:
    resp: Response = client.get("/api/report", params=period or OCT)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def line(report: dict[str, Any], name: str) -> dict[str, Any]:
    found: dict[str, Any] = next(c for c in report["categories"] if c["name"] == name)
    return found


def db_of(app: FastAPI) -> Database:
    db: Database = app.state.db
    return db


def test_requires_login(client: TestClient) -> None:
    assert client.get("/api/report", params=OCT).status_code == 401


@pytest.mark.parametrize("period", [{"start": "2026-10-31", "end": "2026-10-01"},
                                    {"start": "2026-13-01", "end": "2026-10-31"}, {"start": "2026-10-01"}])
def test_a_bad_period_is_refused(asha: TestClient, period: dict[str, str]) -> None:
    assert asha.get("/api/report", params=period).status_code == 422


def test_a_category_total_is_the_sum_of_its_payees_and_their_transactions(asha: TestClient, accts: dict[str, str]) -> None:
    food: str = category(asha, "Food")
    enter(asha, amount_paise=40000, payee="Cafe", category_id=food, from_account_id=accts["bank"])
    enter(asha, amount_paise=25000, payee="Cafe", from_account_id=accts["card"])  # Cafe's rule is Food now
    enter(asha, amount_paise=12000, payee="Bakery", category_id=food, from_account_id=accts["cash"])
    food_line: dict[str, Any] = line(get(asha), "Food")
    assert (food_line["amount_paise"], food_line["count"]) == (77000, 3)
    assert [(p["name"], p["amount_paise"], len(p["transactions"])) for p in food_line["payees"]] == [
        ("Cafe", 65000, 2), ("Bakery", 12000, 1)]
    leaf: dict[str, Any] = food_line["payees"][1]["transactions"][0]
    assert (leaf["amount_paise"], leaf["account_label"], leaf["provenance"]) == (12000, "Wallet", "manual")


def test_empty_period_still_has_the_uncategorized_line(asha: TestClient) -> None:
    report: dict[str, Any] = get(asha)
    assert report["categories"] == [{"category_id": None, "name": "Not categorized yet", "amount_paise": 0,
                                     "count": 0, "payees": []}]
    assert (report["spending_paise"], report["cash_out_paise"], report["money_in_paise"]) == (0, 0, 0)


def test_uncategorized_spending_is_its_own_line_and_counts_in_spending(asha: TestClient, accts: dict[str, str]) -> None:
    food: str = category(asha, "Food")
    enter(asha, amount_paise=30000, payee="Cafe", category_id=food, from_account_id=accts["bank"])
    enter(asha, amount_paise=1200000, payee="Somewhere", from_account_id=accts["bank"])
    report: dict[str, Any] = get(asha)
    assert [(c["name"], c["amount_paise"]) for c in report["categories"]] == [
        ("Food", 30000), ("Not categorized yet", 1200000)]  # uncategorized last even when biggest
    assert report["spending_paise"] == 1230000


def test_a_refund_reduces_its_category_and_is_not_income(asha: TestClient, accts: dict[str, str]) -> None:
    food: str = category(asha, "Food")
    enter(asha, amount_paise=40000, payee="Cafe", category_id=food, from_account_id=accts["card"])
    enter(asha, kind="refund", amount_paise=15000, payee="Cafe", to_account_id=accts["card"])
    report: dict[str, Any] = get(asha)
    assert line(report, "Food")["amount_paise"] == 25000
    assert report["spending_paise"] == 25000
    assert report["money_in_paise"] == 0 and report["income"] == []
    assert sorted(t["amount_paise"] for t in line(report, "Food")["payees"][0]["transactions"]) == [-15000, 40000]


def test_refund_larger_than_spending_goes_negative(asha: TestClient, accts: dict[str, str]) -> None:
    food: str = category(asha, "Food")
    enter(asha, amount_paise=40000, payee="Cafe", category_id=food, from_account_id=accts["card"],
          transaction_date="2026-09-28")
    enter(asha, kind="refund", amount_paise=40000, payee="Cafe", to_account_id=accts["card"])
    report: dict[str, Any] = get(asha)
    assert line(report, "Food")["amount_paise"] == -40000
    assert report["spending_paise"] == -40000 == sum(c["amount_paise"] for c in report["categories"])


def test_a_card_bill_payment_is_a_transfer_in_cash_out_but_not_spending_or_money_in(
        asha: TestClient, accts: dict[str, str]) -> None:
    enter(asha, amount_paise=300000, payee="Grocer", from_account_id=accts["card"])
    enter(asha, kind="transfer", amount_paise=2800000, from_account_id=accts["bank"], to_account_id=accts["card"])
    report: dict[str, Any] = get(asha)
    assert [(t["kind"], t["amount_paise"], t["from_label"], t["to_label"]) for t in report["transfers"]] == [
        ("card_bill", 2800000, "Bank", "Card")]
    assert report["spending_paise"] == 300000  # the card swipe, not the bill
    assert report["money_in_paise"] == 0
    assert report["cash_out_paise"] == 2800000  # the bill left the bank; the swipe never touched it
    assert sum(c["amount_paise"] for c in report["categories"]) == 300000


def test_cash_out_counts_bank_and_cash_spending_and_nets_refunds_into_them(
        asha: TestClient, accts: dict[str, str]) -> None:
    enter(asha, amount_paise=50000, payee="A", from_account_id=accts["bank"])
    enter(asha, amount_paise=7000, payee="B", from_account_id=accts["cash"])
    enter(asha, amount_paise=90000, payee="C", from_account_id=accts["card"])  # card: not cash out
    enter(asha, kind="refund", amount_paise=5000, payee="A", to_account_id=accts["bank"])
    enter(asha, kind="refund", amount_paise=9000, payee="C", to_account_id=accts["card"])  # card: not cash out
    enter(asha, kind="transfer", amount_paise=200000, from_account_id=accts["bank"], to_account_id=accts["cash"])
    report: dict[str, Any] = get(asha)
    assert report["cash_out_paise"] == 50000 + 7000 - 5000  # the ATM withdrawal moved money, nothing left
    assert report["spending_paise"] == 50000 + 7000 + 90000 - 5000 - 9000
    assert [t["kind"] for t in report["transfers"]] == ["cash_withdrawal"]


def test_spending_with_no_known_account_is_reported_apart_from_cash_out(
        app: FastAPI, asha: TestClient, accts: dict[str, str]) -> None:
    txn: str = enter(asha, amount_paise=60000, payee="Toll", from_account_id=accts["bank"])
    with db_of(app).write_session() as s:  # as an email whose account number isn't one of theirs
        s.execute(update(Transaction).where(Transaction.id == txn).values(
            from_account_id=None, unresolved_from_identifier="9999"))
    report: dict[str, Any] = get(asha)
    assert (report["spending_paise"], report["cash_out_paise"], report["cash_out_unknown_paise"]) == (60000, 0, 60000)
    assert report["categories"][0]["payees"][0]["transactions"][0]["account_label"] == "ending 9999"


def test_income_is_money_in_by_payee(asha: TestClient, accts: dict[str, str]) -> None:
    enter(asha, kind="income", amount_paise=9000000, payee="Employer", to_account_id=accts["bank"])
    report: dict[str, Any] = get(asha)
    assert report["money_in_paise"] == 9000000
    assert [(p["name"], p["amount_paise"]) for p in report["income"]] == [("Employer", 9000000)]
    assert report["spending_paise"] == 0


def test_reversed_payment_counts_nowhere_but_is_listed(app: FastAPI, asha: TestClient, accts: dict[str, str]) -> None:
    txn: str = enter(asha, amount_paise=70000, payee="Shop", from_account_id=accts["bank"])
    with db_of(app).write_session() as s:
        s.execute(update(Transaction).where(Transaction.id == txn).values(status="reversed"))
    report: dict[str, Any] = get(asha)
    assert (report["spending_paise"], report["cash_out_paise"]) == (0, 0)
    assert [t["id"] for t in report["reversed"]] == [txn]


def test_archived_category_falls_into_uncategorized(app: FastAPI, asha: TestClient, accts: dict[str, str]) -> None:
    food: str = category(asha, "Food")
    enter(asha, amount_paise=40000, payee="Cafe", category_id=food, from_account_id=accts["bank"])
    with db_of(app).write_session() as s:
        s.execute(update(Category).where(Category.id == food).values(archived_at="2026-10-10T00:00:00Z"))
    report: dict[str, Any] = get(asha)
    assert [(c["name"], c["amount_paise"]) for c in report["categories"]] == [("Not categorized yet", 40000)]


def test_period_is_inclusive_of_both_ends(asha: TestClient, accts: dict[str, str]) -> None:
    for day in ("2026-08-31", "2026-09-01", "2026-09-30", "2026-10-01"):  # manual entries can't be future-dated
        enter(asha, amount_paise=100, payee="Shop", from_account_id=accts["bank"], transaction_date=day)
    assert get(asha, start="2026-09-01", end="2026-09-30")["spending_paise"] == 200
    assert get(asha, start="2026-09-30", end="2026-09-30")["spending_paise"] == 100


def test_the_breakdown_adds_up_at_every_level(asha: TestClient, accts: dict[str, str]) -> None:
    """§8.4's surviving check: the reporting code is internally consistent."""
    food: str = category(asha, "Food")
    travel: str = category(asha, "Travel")
    enter(asha, amount_paise=40000, payee="Cafe", category_id=food, from_account_id=accts["bank"])
    enter(asha, amount_paise=81000, payee="Rail", category_id=travel, from_account_id=accts["card"])
    enter(asha, kind="refund", amount_paise=1000, payee="Rail", to_account_id=accts["card"])
    enter(asha, amount_paise=333, payee="Mystery", from_account_id=accts["cash"])
    report: dict[str, Any] = get(asha)
    for c in report["categories"]:
        assert c["amount_paise"] == sum(p["amount_paise"] for p in c["payees"])
        for p in c["payees"]:
            assert p["amount_paise"] == sum(t["amount_paise"] for t in p["transactions"])
    assert report["spending_paise"] == sum(c["amount_paise"] for c in report["categories"]) == 40000 + 81000 - 1000 + 333


def test_another_users_transactions_never_show(app: FastAPI, asha: TestClient, accts: dict[str, str]) -> None:
    enter(asha, amount_paise=40000, payee="Cafe", from_account_id=accts["bank"])
    asha.post("/api/auth/logout")
    make_user(app, "ravi", "ravi password 1")
    login(asha, "ravi", "ravi password 1")
    assert get(asha)["spending_paise"] == 0


def test_spending_from_a_number_marked_not_mine_stays_out_of_both_cash_figures(
        app: FastAPI, asha: TestClient, accts: dict[str, str]) -> None:
    """A toll paid from a wallet the user said isn't theirs: spending, but nothing to assign, so no warning."""
    txn: str = enter(asha, amount_paise=12500, payee="Toll", from_account_id=accts["bank"])
    with db_of(app).write_session() as s:
        s.execute(update(Transaction).where(Transaction.id == txn).values(
            from_account_id=None, unresolved_from_identifier="WALLET 00001234"))
    assert asha.post("/api/review/not-my-account", json={"identifier": "WALLET 00001234"}).status_code == 200
    report: dict[str, Any] = get(asha)
    assert (report["spending_paise"], report["cash_out_paise"], report["cash_out_unknown_paise"]) == (12500, 0, 0)


def test_a_card_bill_with_no_known_payer_is_reported_apart_from_cash_out(
        app: FastAPI, asha: TestClient, accts: dict[str, str]) -> None:
    """The card says it was paid; which account paid is still a review question, so it can't be cash out yet."""
    txn: str = enter(asha, kind="transfer", amount_paise=2800000, from_account_id=accts["bank"],
                     to_account_id=accts["card"])
    with db_of(app).write_session() as s:
        s.execute(update(Transaction).where(Transaction.id == txn).values(from_account_id=None))
    report: dict[str, Any] = get(asha)
    assert [(t["kind"], t["from_label"]) for t in report["transfers"]] == [("card_bill", None)]
    assert (report["cash_out_paise"], report["cash_out_unknown_paise"], report["spending_paise"]) == (0, 2800000, 0)
