"""CSV export per user per period (tech-design §9.7): the transactions as the app shows them now — edits applied,
categories resolved by today's rules. Every name and amount here is invented."""

import csv
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Connection

from hisaab.db import Database
from hisaab.reports.export import COLUMNS, export_rows
from tests import factories as f
from tests.conftest import login, make_user


@pytest.fixture
def people(conn: Connection) -> tuple[str, str]:
    asha: str = f.user(conn)["id"]
    ravi: str = f.user(conn)["id"]
    conn.commit()
    return asha, ravi


def seed(conn: Connection, uid: str) -> dict[str, str]:
    bank: str = f.account(conn, uid, display_name="Savings", type="bank")["id"]
    card: str = f.account(conn, uid, display_name="Rewards Card", type="credit_card")["id"]
    food: str = f.category(conn, uid, name="Food")["id"]
    grocer: str = f.payee(conn, normalized_name='Corner, "Grocer"\nBLR')["id"]
    lunch: str = f.transaction(conn, uid, transaction_date="2026-10-03", transaction_time="13:05:00", amount=125050,
                               kind="expense", from_account_id=card, payee_raw="POS CORNER GROCER", payee_id=grocer,
                               reference_number="R-1")["id"]
    f.transaction_edit(conn, lunch, uid, field="category_override_id", new_value=food)
    salary: str = f.transaction(conn, uid, transaction_date="2026-10-01", amount=9000000, kind="income",
                                to_account_id=bank, payee_raw="EMPLOYER LTD")["id"]
    f.transaction(conn, uid, transaction_date="2026-10-10", amount=500000, kind="transfer", from_account_id=bank,
                  to_account_id=card)
    refund: str = f.transaction(conn, uid, transaction_date="2026-10-12", amount=20000, kind="refund",
                                to_account_id=card, payee_raw="POS CORNER GROCER", payee_id=grocer)["id"]
    f.transaction_edit(conn, refund, uid, field="amount", new_value="19999")  # the user's correction wins
    f.transaction(conn, uid, transaction_date="2026-09-30", amount=100, kind="expense", from_account_id=bank)  # outside
    conn.commit()
    return {"lunch": lunch, "salary": salary}


def test_the_rows_are_the_period_as_the_app_shows_it(db: Database, conn: Connection, people: tuple[str, str]) -> None:
    asha, ravi = people
    seed(conn, asha)
    f.transaction(conn, ravi, transaction_date="2026-10-05", amount=777, kind="expense")  # someone else's
    conn.commit()
    with db.read_session() as s:
        rows: list[dict[str, str]] = export_rows(s, asha, "2026-10-01", "2026-10-31")
    assert COLUMNS == ("Date", "Time", "Type", "Amount", "From account", "To account", "Payee", "Text from the bank",
                       "Category", "Status", "Reference")
    assert [(r["Date"], r["Type"], r["Amount"]) for r in rows] == [
        ("2026-10-01", "income", "90000.00"), ("2026-10-03", "expense", "1250.50"),
        ("2026-10-10", "transfer", "5000.00"), ("2026-10-12", "refund", "199.99")]
    lunch: dict[str, str] = rows[1]
    assert (lunch["Time"], lunch["From account"], lunch["To account"], lunch["Payee"], lunch["Text from the bank"],
            lunch["Category"], lunch["Status"], lunch["Reference"]) == (
        "13:05", "Rewards Card", "", 'Corner, "Grocer"\nBLR', "POS CORNER GROCER", "Food", "confirmed", "R-1")
    assert (rows[2]["From account"], rows[2]["To account"]) == ("Savings", "Rewards Card")


@pytest.fixture
def asha_client(app: FastAPI, client: TestClient) -> TestClient:
    make_user(app, "asha", "asha password 1")
    assert login(client, "asha", "asha password 1").status_code == 200
    return client


def test_the_download_is_quoted_csv_with_a_month_name(asha_client: TestClient, conn: Connection) -> None:
    seed(conn, asha_client.get("/api/auth/me").json()["id"])
    resp: Response = asha_client.get("/api/export/transactions.csv", params={"start": "2026-10-01", "end": "2026-10-31"})
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"] == 'attachment; filename="hisaab-2026-10.csv"'
    parsed: list[list[str]] = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    assert parsed[0] == list(COLUMNS) and len(parsed) == 5
    assert parsed[2][6] == 'Corner, "Grocer"\nBLR'  # commas, quotes and a newline survive the round trip


def test_a_part_month_gets_a_range_name_and_a_bad_range_is_refused(asha_client: TestClient) -> None:
    resp: Response = asha_client.get("/api/export/transactions.csv", params={"start": "2026-10-01", "end": "2026-10-15"})
    assert resp.headers["content-disposition"] == 'attachment; filename="hisaab-2026-10-01_2026-10-15.csv"'
    assert asha_client.get("/api/export/transactions.csv", params={"start": "2026-10-15", "end": "2026-10-01"}).status_code == 422
    assert asha_client.get("/api/export/transactions.csv", params={"start": "x", "end": "y"}).status_code == 422


def test_anonymous_gets_401(client: TestClient) -> None:
    assert client.get("/api/export/transactions.csv", params={"start": "2026-10-01", "end": "2026-10-31"}).status_code == 401
