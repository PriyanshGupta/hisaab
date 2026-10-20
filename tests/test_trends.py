"""Monthly trends per category (PRD §7.7; tech-design §8.3; rulings 2026-10-20).

The 12 India months ending this month. A month before the user's history starts is None (no line, not a
zero); inside it, a month with no spending is 0. Categories resolve as the report does (refunds net into
their own category, reversed payments count nowhere). The first, partly covered month and the running
month are partial.
"""

from datetime import date
from typing import Any

import pytest
from sqlalchemy import Connection

from hisaab.db import Database
from hisaab.reports.trends import CategoryTrend, Trends, trends
from tests import factories as f

TODAY: date = date(2026, 10, 20)
MONTHS: list[str] = ["2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06",
                     "2026-07", "2026-08", "2026-09", "2026-10"]


@pytest.fixture
def uid(conn: Connection) -> str:
    user_id: str = f.user(conn)["id"]
    conn.commit()
    return user_id


def category(conn: Connection, uid: str, name: str) -> str:
    cat_id: str = f.category(conn, uid, name=name)["id"]
    conn.commit()
    return cat_id


def spend(conn: Connection, uid: str, day: str, amount: int, cat: str | None = None, kind: str = "expense",
          **kw: Any) -> str:
    txn_id: str = f.transaction(conn, uid, transaction_date=day, amount=amount, kind=kind, **kw)["id"]
    if cat is not None:
        f.transaction_edit(conn, txn_id, uid, field="category_override_id", new_value=cat)
    conn.commit()
    return txn_id


def get(db: Database, uid: str, today: date = TODAY) -> Trends:
    with db.read_session() as s:
        return trends(s, uid, today)


def by_name(t: Trends) -> dict[str, CategoryTrend]:
    return {c.name: c for c in t.categories}


def test_twelve_months_ending_this_month_with_history_and_partial_months(db: Database, conn: Connection,
                                                                         uid: str) -> None:
    food: str = category(conn, uid, "Food")
    spend(conn, uid, "2026-07-12", 100000, food)
    spend(conn, uid, "2026-09-03", 200000, food)
    spend(conn, uid, "2026-10-02", 50000, food)
    spend(conn, uid, "2026-10-05", 20000, food, kind="refund")  # nets into Food
    t: Trends = get(db, uid)
    assert (t.months, t.history_from, t.partial) == (MONTHS, "2026-07-12", ["2026-07", "2026-10"])
    assert by_name(t)["Food"].months == [None] * 8 + [100000, 0, 200000, 30000]
    assert by_name(t)["Food"].total == 330000


def test_history_starting_on_the_first_leaves_only_this_month_partial(db: Database, conn: Connection,
                                                                      uid: str) -> None:
    spend(conn, uid, "2026-08-01", 1000)
    assert get(db, uid).partial == ["2026-10"]


def test_on_the_first_of_a_month_it_is_in_the_window_and_partial(db: Database, conn: Connection, uid: str) -> None:
    spend(conn, uid, "2026-08-01", 1000)
    t: Trends = get(db, uid, date(2026, 11, 1))
    assert len(t.months) == 12 and t.months[0] == "2025-12" and t.months[-1] == "2026-11"
    assert t.partial == ["2026-11"] and t.categories[0].months[-1] == 0


def test_a_refunds_only_month_is_negative(db: Database, conn: Connection, uid: str) -> None:
    shop: str = category(conn, uid, "Shopping")
    spend(conn, uid, "2026-08-10", 50000, shop)
    spend(conn, uid, "2026-09-10", 12000, shop, kind="refund")
    assert by_name(get(db, uid))["Shopping"].months[-3:] == [50000, -12000, 0]


def test_categories_by_total_and_uncategorized_last(db: Database, conn: Connection, uid: str) -> None:
    rent: str = category(conn, uid, "Rent")
    cafe: str = category(conn, uid, "Coffee")
    spend(conn, uid, "2026-08-01", 2400000, rent)
    spend(conn, uid, "2026-08-02", 30000, cafe)
    spend(conn, uid, "2026-08-03", 9000000)  # bigger than both, but not categorized yet
    assert [c.name for c in get(db, uid).categories] == ["Rent", "Coffee", "Not categorized yet"]
    assert get(db, uid).categories[-1].category_id is None


def test_a_category_that_spent_nothing_gets_no_chart(db: Database, conn: Connection, uid: str) -> None:
    idle: str = category(conn, uid, "Idle")
    spend(conn, uid, "2026-08-02", 30000)
    spend(conn, uid, "2024-01-02", 30000, idle)  # long before the window
    assert "Idle" not in by_name(get(db, uid))


def test_only_spending_counts(db: Database, conn: Connection, uid: str) -> None:
    food: str = category(conn, uid, "Food")
    bank: str = f.account(conn, uid)["id"]
    card: str = f.account(conn, uid, type="credit_card")["id"]
    conn.commit()
    spend(conn, uid, "2026-08-02", 30000, food)
    spend(conn, uid, "2026-08-03", 900000, food, kind="income", to_account_id=bank)
    spend(conn, uid, "2026-08-04", 500000, food, kind="transfer", from_account_id=bank, to_account_id=card)
    spend(conn, uid, "2026-08-05", 7000, food, kind="adjustment", to_account_id=bank)
    spend(conn, uid, "2026-08-06", 4000, food, status="reversed")
    assert by_name(get(db, uid))["Food"].months[-3:] == [30000, 0, 0]


def test_no_data_means_twelve_empty_months(db: Database, uid: str) -> None:
    t: Trends = get(db, uid)
    assert (t.months, t.history_from, t.partial, t.categories) == (MONTHS, None, ["2026-10"], [])


def test_another_users_spending_never_shows(db: Database, conn: Connection, uid: str) -> None:
    stranger: str = f.user(conn)["id"]
    conn.commit()
    spend(conn, stranger, "2026-08-02", 30000)
    assert get(db, uid).categories == [] and get(db, uid).history_from is None


def test_a_date_edited_to_before_the_first_transaction_moves_the_history_start(db: Database, conn: Connection,
                                                                               uid: str) -> None:
    food: str = category(conn, uid, "Food")
    spend(conn, uid, "2026-07-12", 100000, food)
    moved: str = spend(conn, uid, "2026-07-20", 30000, food)
    f.transaction_edit(conn, moved, uid, field="transaction_date", new_value="2026-06-28", prior_value="2026-07-20")
    conn.commit()
    t: Trends = get(db, uid)
    assert t.history_from == "2026-06-28" and "2026-06" in t.partial
    assert by_name(t)["Food"].months[7:9] == [30000, 100000]  # June shows the money, as the report does
