"""The ledger behind balances and the check (tech-design §8.3, §8.4; rulings 2026-10-20).

Bank and cash: money in is +. Card: debt is + (PRD §10.3), so a purchase adds and a payment takes away.
An opening balance is the balance at the START of its date; an observation, at the END of its date.
Each observation is checked against the anchor before it plus the movements between: the re-anchoring
that keeps an open gap on the interval where it arose.
"""

from typing import Any

import pytest
from sqlalchemy import Connection

from hisaab.balances.ledger import Check, Figure, anchors, checks, figure
from hisaab.db import Database
from hisaab.models import Account
from tests import factories as f



def eod(day: str) -> str:
    """The end of an India day as the UTC instant observations store (18:29:59Z the same date)."""
    return f"{day}T18:29:59.000Z"


@pytest.fixture
def uid(conn: Connection) -> str:
    user_id: str = f.user(conn)["id"]
    conn.commit()
    return user_id


def account(conn: Connection, uid: str, kind: str = "bank", opening: int | None = 100000,
            on: str | None = "2026-07-01") -> str:
    acct_id: str = f.account(conn, uid, type=kind, opening_balance=opening,
                             opening_balance_date=on if opening is not None else None)["id"]
    conn.commit()
    return acct_id


def txn(conn: Connection, uid: str, day: str, amount: int, *, frm: str | None = None, to: str | None = None,
        kind: str | None = None, **kw: Any) -> str:
    kind = kind or ("transfer" if frm and to else "expense" if frm else "income")
    row_id: str = f.transaction(conn, uid, transaction_date=day, amount=amount, kind=kind, from_account_id=frm,
                                to_account_id=to, **kw)["id"]
    conn.commit()
    return row_id


def observe(conn: Connection, uid: str, acct: str, day: str, balance: int, source: str = "statement") -> str:
    obs_id: str = f.balance_observation(conn, user_id=uid, account_id=acct, balance=balance, observed_at=eod(day),
                                        source=source)["id"]
    conn.commit()
    return obs_id


def get(db: Database, uid: str, acct: str, fn: Any) -> Any:
    with db.read_session() as s:
        return fn(s, uid, s.get_one(Account, acct))


def test_bank_balance_is_opening_plus_money_in_minus_money_out(db: Database, conn: Connection, uid: str) -> None:
    bank: str = account(conn, uid)
    txn(conn, uid, "2026-07-05", 50000, to=bank)
    txn(conn, uid, "2026-07-06", 20000, frm=bank)
    fig: Figure = get(db, uid, bank, figure)
    assert (fig.balance, fig.transactions_since, fig.movement_since) == (130000, 2, 30000)
    assert fig.anchor is not None and (fig.anchor.source, fig.anchor.on) == ("opening", "2026-07-01")


def test_the_opening_days_own_transactions_count_after_it(db: Database, conn: Connection, uid: str) -> None:
    bank: str = account(conn, uid)
    txn(conn, uid, "2026-07-01", 30000, frm=bank)  # the opening balance is the START of 1 Jul
    txn(conn, uid, "2026-06-30", 99900, frm=bank)  # before the anchor: not counted
    assert get(db, uid, bank, figure).balance == 70000


def test_card_debt_goes_up_with_purchases_and_down_with_payments_and_refunds(db: Database, conn: Connection,
                                                                             uid: str) -> None:
    card: str = account(conn, uid, "credit_card", opening=500000)
    bank: str = account(conn, uid)
    txn(conn, uid, "2026-07-03", 120000, frm=card)                       # purchase: owe more
    txn(conn, uid, "2026-07-04", 20000, to=card, kind="refund")          # refund: owe less
    txn(conn, uid, "2026-07-05", 300000, frm=bank, to=card)              # bill paid from the bank
    assert get(db, uid, card, figure).balance == 500000 + 120000 - 20000 - 300000


def test_a_bill_payment_moves_both_accounts_once(db: Database, conn: Connection, uid: str) -> None:
    card: str = account(conn, uid, "credit_card", opening=300000)
    bank: str = account(conn, uid, opening=1000000)
    txn(conn, uid, "2026-07-05", 300000, frm=bank, to=card)
    assert (get(db, uid, bank, figure).balance, get(db, uid, card, figure).balance) == (700000, 0)


def test_an_observation_is_checked_against_the_anchor_before_it(db: Database, conn: Connection, uid: str) -> None:
    bank: str = account(conn, uid)
    txn(conn, uid, "2026-07-10", 10000, frm=bank)
    first: str = observe(conn, uid, bank, "2026-07-31", 85000)   # expected 90000: ₹50 less than Hisaab has
    txn(conn, uid, "2026-08-10", 5000, frm=bank)
    second: str = observe(conn, uid, bank, "2026-08-31", 80000)  # from 85000 (the bank's figure): expected 80000
    found: list[Check] = get(db, uid, bank, checks)
    assert [(c.observation_id, c.expected, c.gap, c.since.on) for c in found] == [
        (first, 90000, -5000, "2026-07-01"), (second, 80000, 0, "2026-07-31")]


def test_an_observation_counts_its_own_days_transactions(db: Database, conn: Connection, uid: str) -> None:
    bank: str = account(conn, uid)
    txn(conn, uid, "2026-07-31", 10000, frm=bank)  # the observation is the END of 31 Jul
    txn(conn, uid, "2026-08-01", 77700, frm=bank)  # after it
    observe(conn, uid, bank, "2026-07-31", 90000)
    [check] = get(db, uid, bank, checks)
    assert (check.expected, check.gap) == (90000, 0)


def test_first_observation_without_an_opening_balance_is_not_checked(db: Database, conn: Connection,
                                                                     uid: str) -> None:
    card: str = account(conn, uid, "credit_card", opening=None)
    txn(conn, uid, "2026-07-05", 10000, frm=card)
    first: str = observe(conn, uid, card, "2026-07-31", 10000)
    txn(conn, uid, "2026-08-05", 20000, frm=card)
    second: str = observe(conn, uid, card, "2026-08-31", 31000)
    [check] = get(db, uid, card, checks)
    assert (check.observation_id, check.since.observation_id, check.gap) == (second, first, 1000)
    fig: Figure = get(db, uid, card, figure)
    assert fig.anchor is not None and (fig.anchor.on, fig.balance, fig.transactions_since) == ("2026-08-31", 31000, 0)


def test_an_observation_before_the_opening_balance_is_not_checked(db: Database, conn: Connection, uid: str) -> None:
    bank: str = account(conn, uid)
    observe(conn, uid, bank, "2026-06-15", 123400)
    assert get(db, uid, bank, checks) == []
    fig: Figure = get(db, uid, bank, figure)
    assert fig.anchor is not None and fig.anchor.source == "opening"


def test_no_opening_and_no_observation_means_nothing_to_start_from(db: Database, conn: Connection, uid: str) -> None:
    cash: str = account(conn, uid, "cash", opening=None)
    txn(conn, uid, "2026-07-05", 10000, frm=cash)
    fig: Figure = get(db, uid, cash, figure)
    assert (fig.anchor, fig.balance) == (None, None) and get(db, uid, cash, anchors) == []


def test_edits_count_through_the_effective_view(db: Database, conn: Connection, uid: str) -> None:
    bank: str = account(conn, uid)
    spent: str = txn(conn, uid, "2026-07-05", 10000, frm=bank)
    f.transaction_edit(conn, spent, uid, field="amount", new_value="25000", prior_value="10000")
    conn.commit()
    assert get(db, uid, bank, figure).balance == 75000


def test_unresolved_legs_touch_no_account(db: Database, conn: Connection, uid: str) -> None:
    bank: str = account(conn, uid)
    txn(conn, uid, "2026-07-05", 10000, kind="expense", unresolved_from_identifier="9999")
    assert get(db, uid, bank, figure).balance == 100000


def test_other_users_transactions_never_count(db: Database, conn: Connection, uid: str) -> None:
    bank: str = account(conn, uid)
    stranger: str = f.user(conn)["id"]
    conn.commit()
    txn(conn, stranger, "2026-07-05", 10000, frm=bank)  # a corrupt row naming someone else's account
    assert get(db, uid, bank, figure).balance == 100000
