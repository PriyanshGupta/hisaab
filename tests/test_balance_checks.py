"""The gap lifecycle (tech-design §8.4; build-plan Slice 20's four checks, synthetic): open, investigated, accepted.

A gap is the live difference between an observation and the anchor before it plus the movements between.
A row records it (open), and ends investigated (fixed elsewhere, nothing written: only once the live gap is
back within tolerance) or accepted (a visible adjustment that counts in no total). A new observation never
absorbs an open gap: each is checked from the anchor before it.
"""

from datetime import timedelta

import pytest
from sqlalchemy import Connection, func, select

from hisaab.balances.checks import CheckError, accept, add_checkpoint, close, record_checks
from hisaab.balances.ledger import Check, Figure, checks, figure, signed
from hisaab.clock import local_today
from hisaab.db import Database
from hisaab.models import Account, BalanceDelta, Transaction, TransactionEffective
from hisaab.reports.spending import ReportOut, report
from hisaab.transactions import repository as txns
from tests import factories as f
from tests.test_balances_ledger import account, observe, txn


@pytest.fixture
def uid(conn: Connection) -> str:
    user_id: str = f.user(conn)["id"]
    conn.commit()
    return user_id


def rows(db: Database, uid: str) -> list[BalanceDelta]:
    with db.read_session() as s:
        found: list[BalanceDelta] = list(s.scalars(select(BalanceDelta).where(BalanceDelta.user_id == uid)
                                                   .order_by(BalanceDelta.created_at, BalanceDelta.id)))
        s.expunge_all()
        return found


def live(db: Database, uid: str, acct: str) -> list[Check]:
    with db.read_session() as s:
        return checks(s, uid, s.get_one(Account, acct))


def adjustments(db: Database, uid: str) -> int:
    with db.read_session() as s:
        n: int | None = s.scalar(select(func.count()).select_from(Transaction).where(
            Transaction.user_id == uid, Transaction.kind == "adjustment"))
        return n or 0


def card_with_paid_bill(conn: Connection, uid: str) -> tuple[str, str, str, str]:
    """A card statement that matches the ledger: 5,000 owed, 1,000 spent, the 3,000 bill paid from the bank."""
    card: str = account(conn, uid, "credit_card", opening=500000)
    bank: str = account(conn, uid, opening=1000000)
    txn(conn, uid, "2026-07-05", 100000, frm=card)
    bill: str = txn(conn, uid, "2026-07-10", 300000, frm=bank, to=card)
    obs: str = observe(conn, uid, card, "2026-07-31", 300000)
    return card, bank, bill, obs


def test_a_misclassified_bill_payment_flags_the_card_gap_at_its_amount(db: Database, conn: Connection,
                                                                       uid: str) -> None:
    card, _, bill, obs = card_with_paid_bill(conn, uid)
    assert [c.gap for c in live(db, uid, card)] == [0]
    with db.write_session() as s:
        txns.apply_edits(s, uid, bill, {"kind": "expense", "to_account_id": None})  # the bill booked as spending
        assert record_checks(s, uid) == 1
    [row] = rows(db, uid)
    assert (row.account_id, row.observation_id, row.delta, row.status) == (card, obs, -300000, "open")
    assert (row.observed_balance, row.computed_balance, row.arose_from) == (300000, 600000, "2026-07-01")


def test_fixing_it_lets_the_delta_close_as_investigated_with_no_adjustment(db: Database, conn: Connection,
                                                                           uid: str) -> None:
    card, _, bill, obs = card_with_paid_bill(conn, uid)
    with db.write_session() as s:
        txns.apply_edits(s, uid, bill, {"kind": "expense", "to_account_id": None})
        record_checks(s, uid)
    with db.write_session() as s:
        txns.revert(s, uid, bill, ["kind", "to_account_id"])  # the cause found and fixed
        closed: BalanceDelta = close(s, uid, obs)
        assert (closed.status, closed.adjustment_transaction_id) == ("investigated", None)
        assert closed.resolved_at is not None
    assert adjustments(db, uid) == 0 and [c.gap for c in live(db, uid, card)] == [0]


def test_close_is_refused_while_the_gap_remains(db: Database, conn: Connection, uid: str) -> None:
    _, _, bill, obs = card_with_paid_bill(conn, uid)
    with db.write_session() as s:
        txns.apply_edits(s, uid, bill, {"kind": "expense", "to_account_id": None})
        record_checks(s, uid)
    with pytest.raises(CheckError, match="The gap is still there"), db.write_session() as s:
        close(s, uid, obs)
    assert [r.status for r in rows(db, uid)] == ["open"]


def test_accepting_writes_a_visible_adjustment_outside_every_total(db: Database, conn: Connection, uid: str) -> None:
    bank: str = account(conn, uid)
    txn(conn, uid, "2026-08-03", 20000, frm=bank, payee_raw="Shop")
    with db.write_session() as s:
        before: ReportOut = report(s, uid, "2026-08-01", "2026-08-31")
        obs: str = add_checkpoint(s, uid, bank, 70000, "2026-08-20").id  # expected 80,000: ₹100 unexplained
        row: BalanceDelta = accept(s, uid, obs)
        assert row.status == "accepted" and row.adjustment_transaction_id is not None
        made: TransactionEffective = s.scalars(select(TransactionEffective).where(
            TransactionEffective.id == row.adjustment_transaction_id)).one()
        assert (made.kind, made.amount, made.from_account_id, made.to_account_id, made.transaction_date) == (
            "adjustment", 10000, bank, None, "2026-08-20")
        after: ReportOut = report(s, uid, "2026-08-01", "2026-08-31")
    assert (after.spending_paise, after.cash_out_paise, after.money_in_paise) == (
        before.spending_paise, before.cash_out_paise, before.money_in_paise)
    assert [c.gap for c in live(db, uid, bank)] == [0]


@pytest.mark.parametrize(("kind", "observed", "frm", "to"), [
    ("bank", 70000, True, False),          # the bank has less than Hisaab: money out
    ("bank", 130000, False, True),         # more: money in
    ("credit_card", 130000, True, False),  # owe more: like a purchase
    ("credit_card", 70000, False, True),   # owe less: like a payment
])
def test_the_adjustment_moves_the_balance_by_exactly_the_gap(db: Database, conn: Connection, uid: str, kind: str,
                                                             observed: int, frm: bool, to: bool) -> None:
    acct: str = account(conn, uid, kind, opening=100000)
    with db.write_session() as s:
        obs: str = add_checkpoint(s, uid, acct, observed, "2026-08-20").id
        row: BalanceDelta = accept(s, uid, obs)
        made: TransactionEffective = s.scalars(select(TransactionEffective).where(
            TransactionEffective.id == row.adjustment_transaction_id)).one()
        assert ((made.from_account_id == acct), (made.to_account_id == acct)) == (frm, to)
        assert signed(s.get_one(Account, acct), made) == observed - 100000


def test_accepting_twice_or_accepting_a_match_is_refused(db: Database, conn: Connection, uid: str) -> None:
    bank: str = account(conn, uid)
    with db.write_session() as s:
        obs: str = add_checkpoint(s, uid, bank, 90000, "2026-08-20").id
        accept(s, uid, obs)
    with pytest.raises(CheckError, match="already matches"), db.write_session() as s:
        accept(s, uid, obs)
    assert adjustments(db, uid) == 1


def test_a_new_observation_does_not_absorb_an_open_delta(db: Database, conn: Connection, uid: str) -> None:
    bank: str = account(conn, uid)
    with db.write_session() as s:
        first: str = add_checkpoint(s, uid, bank, 90000, "2026-08-10").id  # ₹100 unexplained since 1 Jul
    txn(conn, uid, "2026-08-15", 5000, frm=bank)
    with db.write_session() as s:
        second: str = add_checkpoint(s, uid, bank, 85000, "2026-08-31").id  # matches, from the bank's 90,000
    [row] = rows(db, uid)
    assert (row.observation_id, row.status, row.delta) == (first, "open", -10000)
    assert [(c.observation_id, c.gap) for c in live(db, uid, bank)] == [(first, -10000), (second, 0)]


def test_record_checks_is_idempotent(db: Database, conn: Connection, uid: str) -> None:
    bank: str = account(conn, uid)
    observe(conn, uid, bank, "2026-08-10", 90000)
    with db.write_session() as s:
        assert (record_checks(s, uid), record_checks(s, uid)) == (1, 0)


def test_a_gap_that_comes_back_after_closing_opens_a_new_row(db: Database, conn: Connection, uid: str) -> None:
    _, _, bill, obs = card_with_paid_bill(conn, uid)
    for _ in range(2):
        with db.write_session() as s:
            txns.apply_edits(s, uid, bill, {"kind": "expense", "to_account_id": None})
            record_checks(s, uid)
        with db.write_session() as s:
            txns.revert(s, uid, bill, ["kind", "to_account_id"])
            close(s, uid, obs)
    assert [r.status for r in rows(db, uid)] == ["investigated", "investigated"]


def test_a_checkpoint_is_a_manual_observation_checked_at_once(db: Database, conn: Connection, uid: str) -> None:
    bank: str = account(conn, uid)
    with db.write_session() as s:
        obs: str = add_checkpoint(s, uid, bank, 90000, "2026-08-10").id
    [row] = rows(db, uid)
    assert row.observation_id == obs and row.delta == -10000


@pytest.mark.parametrize(("kind", "balance", "on", "says"), [
    ("bank", 1000, "tomorrow", "can't be in the future"),
    ("credit_card", -5000, "2026-08-10", "positive amount"),
    ("bank", 1000, "not-a-date", "real date"),
])
def test_checkpoint_refusals(db: Database, conn: Connection, uid: str, kind: str, balance: int, on: str,
                             says: str) -> None:
    acct: str = account(conn, uid, kind)
    day: str = (local_today() + timedelta(days=1)).isoformat() if on == "tomorrow" else on
    with pytest.raises(CheckError, match=says), db.write_session() as s:
        add_checkpoint(s, uid, acct, balance, day)


def test_other_users_observations_and_accounts_are_invisible(db: Database, conn: Connection, uid: str) -> None:
    bank: str = account(conn, uid)
    obs: str = observe(conn, uid, bank, "2026-08-10", 90000)
    stranger: str = f.user(conn)["id"]
    conn.commit()
    for action in (lambda s: accept(s, stranger, obs), lambda s: close(s, stranger, obs),
                   lambda s: add_checkpoint(s, stranger, bank, 1, "2026-08-10")):
        with pytest.raises(CheckError, match="No such"), db.write_session() as s:
            action(s)


# --- found in the whole-branch review: where a balance sits inside its day ------------------------------

def test_a_checkpoint_entered_today_then_a_later_expense_today_leaves_no_gap(db: Database, conn: Connection,
                                                                              uid: str) -> None:
    bank: str = account(conn, uid)
    today: str = local_today().isoformat()
    with db.write_session() as s:
        add_checkpoint(s, uid, bank, 100000, today)  # correct as entered (now, mid-day)
    txn(conn, uid, today, 5000, frm=bank)  # spent after the balance was entered: recorded later
    assert [c.gap for c in live(db, uid, bank)] == [0] and rows(db, uid) == []
    with db.read_session() as s:
        fig: Figure = figure(s, uid, s.get_one(Account, bank))
    assert (fig.balance, fig.transactions_since) == (95000, 1)


def test_two_correct_email_balances_on_one_day_leave_no_gap(db: Database, conn: Connection, uid: str) -> None:
    bank: str = account(conn, uid)
    txn(conn, uid, "2026-08-05", 5000, frm=bank, transaction_time="09:00:00")
    txn(conn, uid, "2026-08-05", 2000, frm=bank, transaction_time="12:00:00")
    f.balance_observation(conn, user_id=uid, account_id=bank, balance=95000, source="email",
                          observed_at="2026-08-05T04:30:00.000Z")  # 10:00 India: after the first only
    f.balance_observation(conn, user_id=uid, account_id=bank, balance=93000, source="email",
                          observed_at="2026-08-05T09:30:00.000Z")  # 15:00 India: after both
    conn.commit()
    assert [c.gap for c in live(db, uid, bank)] == [0, 0]


def test_on_one_date_an_accepted_adjustment_closes_its_own_gap_and_cannot_repeat(db: Database, conn: Connection,
                                                                                 uid: str) -> None:
    bank: str = account(conn, uid)
    observe(conn, uid, bank, "2026-07-05", 100000)                    # a statement: matches
    second: str = observe(conn, uid, bank, "2026-07-05", 95000, "manual")  # same day, ₹50 lower
    assert [c.gap for c in live(db, uid, bank)] == [0, -5000]
    with db.write_session() as s:
        accept(s, uid, second)
    assert [c.gap for c in live(db, uid, bank)] == [0, 0]
    with pytest.raises(CheckError, match="already matches"), db.write_session() as s:
        accept(s, uid, second)
    assert adjustments(db, uid) == 1


def test_a_same_date_checkpoint_replaces_the_earlier_one(db: Database, conn: Connection, uid: str) -> None:
    bank: str = account(conn, uid)
    with db.write_session() as s:
        first: str = add_checkpoint(s, uid, bank, 9000000, "2026-08-10").id  # a typo
        again: str = add_checkpoint(s, uid, bank, 100000, "2026-08-10").id
    assert again == first and [c.gap for c in live(db, uid, bank)] == [0]


def test_card_email_balances_are_never_used(db: Database, conn: Connection, uid: str) -> None:
    """The email contract asks for the "available balance": on a card that is credit left, not what is owed."""
    card: str = account(conn, uid, "credit_card", opening=0)
    f.balance_observation(conn, user_id=uid, account_id=card, balance=5000000, source="email",
                          observed_at="2026-08-05T04:30:00.000Z")
    conn.commit()
    assert live(db, uid, card) == []


def test_a_gap_from_an_edit_is_recorded_on_commit_without_anyone_asking(db: Database, conn: Connection,
                                                                        uid: str) -> None:
    _, _, bill, _ = card_with_paid_bill(conn, uid)
    with db.write_session() as s:
        txns.apply_edits(s, uid, bill, {"kind": "expense", "to_account_id": None})  # no record_checks call
    assert [(r.status, r.delta) for r in rows(db, uid)] == [("open", -300000)]


def test_deleting_an_accepted_adjustment_reopens_its_gap(db: Database, conn: Connection, uid: str) -> None:
    bank: str = account(conn, uid)
    with db.write_session() as s:
        obs: str = add_checkpoint(s, uid, bank, 90000, "2026-08-20").id
        made: str | None = accept(s, uid, obs).adjustment_transaction_id
    assert made is not None
    with db.write_session() as s:
        txns.delete_manual(s, uid, made)
    [row] = rows(db, uid)
    assert (row.status, row.adjustment_transaction_id, row.resolved_at) == ("open", None, None)
    assert [c.gap for c in live(db, uid, bank)] == [-10000]
