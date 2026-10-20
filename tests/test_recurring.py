"""Recurring charge detection (PRD §7.8, §10.1; rulings 2026-10-20).

3+ charges from one payee, at similar amounts (each within 10% of the one before it), with every gap
within ±4 days of the typical gap, and a typical gap of at least a week. A weekly shop whose amounts vary
is not recurring; a monthly subscription is, even when the same payee also sells one-off things.
"""

import json
from datetime import date, timedelta

import pytest
from sqlalchemy import Connection

from hisaab.db import Database
from hisaab.reports.recurring import Charge, Rules, Series, find_series, recurring
from tests import factories as f

START: date = date(2026, 1, 5)


def charges(amounts: list[int], gaps: list[int], payee: str = "p-1", start: date = START) -> list[Charge]:
    days: list[date] = [start]
    for g in gaps:
        days.append(days[-1] + timedelta(days=g))
    assert len(days) == len(amounts)
    return [Charge(txn_id=f"{payee}-{i}", on=d.isoformat(), amount=a, payee_key=payee, payee_name=payee.title(),
                   category_id=None, account_label="Card") for i, (d, a) in enumerate(zip(days, amounts))]


def found(cs: list[Charge]) -> list[tuple[str, int, list[int]]]:
    return [(s.payee_name, s.typical_gap_days, [c.amount for c in s.charges]) for s in find_series(cs, Rules())]


def test_a_monthly_subscription_is_found() -> None:
    [s] = find_series(charges([55500] * 4, [30, 31, 30]), Rules())
    assert (s.typical_gap_days, s.latest_amount, s.first_amount, s.last_on) == (30, 55500, 55500, "2026-04-06")
    assert s.next_on == "2026-05-06" and s.yearly_paise == 55500 * 12


def test_a_weekly_shop_with_varying_amounts_is_not() -> None:
    assert found(charges([120000, 85000, 230000, 150000, 98000], [7, 7, 6, 8])) == []


def test_a_steady_charge_is_found_beside_one_offs() -> None:
    cs: list[Charge] = charges([17500, 17500, 1234500, 17500, 17500], [30, 10, 21, 30])
    assert found(cs) == [("P-1", 30, [17500, 17500, 17500, 17500])]


def test_two_steady_charges_from_one_payee_are_two_series() -> None:
    cheap: list[Charge] = charges([14900] * 3, [30, 30])
    dear: list[Charge] = charges([49900] * 3, [30, 30], start=START + timedelta(days=12))
    assert sorted(found([*cheap, *dear])) == [("P-1", 30, [14900] * 3), ("P-1", 30, [49900] * 3)]


def test_a_bill_drifting_under_ten_percent_a_month_stays_one_series() -> None:
    [s] = find_series(charges([100000, 108000, 115000, 124000], [30, 30, 31]), Rules())
    assert (s.first_amount, s.latest_amount) == (100000, 124000)


def test_a_price_jump_over_ten_percent_starts_again() -> None:
    assert found(charges([55500, 55500, 66600, 66600, 66600], [30, 30, 30, 30])) == [("P-1", 30, [66600] * 3)]


def test_gaps_within_four_days_pass_and_five_fail() -> None:
    assert len(found(charges([1000] * 4, [30, 34, 26]))) == 1
    assert found(charges([1000] * 4, [30, 30, 35])) == []  # typical 30: the 35-day gap is 5 off


def test_a_daily_fee_is_not_recurring() -> None:
    assert found(charges([200] * 10, [1] * 9)) == []


def test_two_charges_are_not_enough() -> None:
    assert found(charges([55500] * 2, [30])) == []


def test_same_day_charges_break_a_monthly_rhythm() -> None:
    assert found(charges([55500] * 4, [30, 0, 30])) == []


def test_payees_are_never_mixed() -> None:
    a: list[Charge] = charges([1000] * 2, [30], payee="a")
    b: list[Charge] = charges([1000], [], payee="b", start=START + timedelta(days=60))
    assert found([*a, *b]) == []


# --- from the database ---------------------------------------------------------------------------------------

@pytest.fixture
def uid(conn: Connection) -> str:
    user_id: str = f.user(conn)["id"]
    conn.commit()
    return user_id


def spend(conn: Connection, uid: str, day: date, amount: int, payee: str = "Streamflix", kind: str = "expense",
          card: str | None = None) -> None:
    f.transaction(conn, uid, transaction_date=day.isoformat(), amount=amount, kind=kind, payee_raw=payee,
                  from_account_id=card if kind == "expense" else None, to_account_id=card if kind != "expense" else None)


def test_recurring_reads_expenses_only_in_the_last_12_months(db: Database, conn: Connection, uid: str) -> None:
    card: str = f.account(conn, uid, type="credit_card", display_name="Rewards Card")["id"]
    today: date = date(2026, 10, 20)
    for months_ago in (3, 2, 1):
        spend(conn, uid, today - timedelta(days=30 * months_ago), 55500, card=card)
        spend(conn, uid, today - timedelta(days=30 * months_ago), 900000, payee="Employer", kind="income", card=card)
    for days_ago in (500, 470, 440):  # a year and more ago: outside the window
        spend(conn, uid, today - timedelta(days=days_ago), 29900, payee="Old Service", card=card)
    stranger: str = f.user(conn)["id"]
    for months_ago in (3, 2, 1):
        spend(conn, stranger, today - timedelta(days=30 * months_ago), 19900, payee="Theirs")
    conn.commit()
    with db.read_session() as s:
        found_series: list[Series] = recurring(s, uid, today)
    assert [(x.payee_name, x.account_label, len(x.charges)) for x in found_series] == [("Streamflix", "Rewards Card", 3)]


def test_rules_come_from_the_tunable(db: Database, conn: Connection, uid: str) -> None:
    today: date = date(2026, 10, 20)
    for months_ago in (3, 2, 1):
        spend(conn, uid, today - timedelta(days=30 * months_ago), 55500)
    f.setting(conn, key="recurring_detection", user_id=uid,
              value=json.dumps({"min_occurrences": 4, "interval_tolerance_days": 4, "amount_tolerance_percent": 10}))
    conn.commit()
    with db.read_session() as s:
        assert recurring(s, uid, today) == []


# --- found in the whole-branch review -------------------------------------------------------------------

TODAY: date = date(2026, 10, 20)


def test_a_subscription_that_stopped_is_not_listed() -> None:
    old: list[Charge] = charges([19900] * 4, [30, 30, 30], start=date(2025, 11, 1))  # last charge 30 Jan
    assert find_series(old, Rules(), today=TODAY) == []


def test_after_a_price_rise_only_the_current_price_is_listed() -> None:
    cs: list[Charge] = charges([55500] * 4 + [66600] * 4, [30] * 7, start=date(2026, 3, 1))
    assert [(s.latest_amount, len(s.charges)) for s in find_series(cs, Rules(), today=TODAY)] == [(66600, 4)]


def test_three_similar_amounts_among_many_shop_visits_are_not_a_subscription() -> None:
    """A shop visited weekly with varying amounts can hold three similar amounts at a steady spacing by chance;
    a series with as many of the payee's other charges inside its span as its own is a coincidence."""
    amounts: list[int] = [80000, 230000, 151000, 81000, 99000, 175000, 79500, 260000]
    cs: list[Charge] = charges(amounts, [7] * 7, start=date(2026, 8, 1))  # the ~₹800s fall 21 days apart
    assert find_series(cs, Rules(), today=TODAY) == []


def test_a_one_off_beside_a_long_subscription_still_leaves_it_found() -> None:
    cs: list[Charge] = charges([17500] * 6 + [1234500] + [17500] * 2, [30, 30, 30, 30, 30, 5, 25, 30],
                               start=date(2026, 3, 15))  # still running on TODAY
    assert [s.latest_amount for s in find_series(cs, Rules(), today=TODAY)] == [17500]


def test_gaps_are_compared_with_the_exact_median_not_a_rounded_one() -> None:
    """Medians 30.5 and 31.5 each leave one gap 4.5 days off: both fail. A rounded median let one through."""
    assert find_series(charges([1000] * 5, [26, 30, 31, 34]), Rules()) == []
    assert find_series(charges([1000] * 5, [27, 31, 32, 35]), Rules()) == []
    assert len(find_series(charges([1000] * 5, [27, 31, 31, 35]), Rules())) == 1  # median 31: all within 4


def test_the_yearly_estimate_counts_charges_per_year_for_named_rhythms() -> None:
    [monthly] = find_series(charges([55500] * 3, [30, 31]), Rules())
    [weekly] = find_series(charges([20000] * 4, [7, 7, 7]), Rules())
    [odd] = find_series(charges([10000] * 3, [45, 45]), Rules())
    assert (monthly.yearly_paise, weekly.yearly_paise, odd.yearly_paise) == (55500 * 12, 20000 * 52, 10000 * 365 // 45)
