"""PRD §7.10's identity, kept as what it really is: a test that the reporting code is internally consistent.

    spending − cash out = the change in card outstanding, derived from the same ledger

Both sides come from the app's own transactions, so this holds on corrupted data too: it can never be a
data check (PRD §7.10, §11.7; tech-design §8.4). The real check compares with a figure the bank produced
(balances/checks.py). If this test fails, the report and the ledger disagree about how money moves.
"""

import pytest
from sqlalchemy import Connection

from hisaab.balances.ledger import signed
from hisaab.db import Database
from hisaab.models import Account, TransactionEffective
from hisaab.reports.spending import ReportOut, report
from tests import factories as f
from tests.test_balances_ledger import account, txn


@pytest.fixture
def uid(conn: Connection) -> str:
    user_id: str = f.user(conn)["id"]
    conn.commit()
    return user_id


def test_spending_minus_cash_out_equals_the_card_ledger_change(db: Database, conn: Connection, uid: str) -> None:
    card: str = account(conn, uid, "credit_card", opening=0)
    bank: str = account(conn, uid, opening=0)
    txn(conn, uid, "2026-08-02", 120000, frm=card, payee_raw="Grocer")             # card purchase
    txn(conn, uid, "2026-08-03", 45000, frm=card, payee_raw="Cinema")              # card purchase
    txn(conn, uid, "2026-08-04", 15000, to=card, kind="refund", payee_raw="Grocer")  # card refund
    txn(conn, uid, "2026-08-05", 30000, frm=bank, payee_raw="Chemist")             # bank expense
    txn(conn, uid, "2026-08-06", 5000, to=bank, kind="refund", payee_raw="Chemist")  # bank refund
    txn(conn, uid, "2026-08-10", 100000, frm=bank, to=card)                        # card bill paid
    txn(conn, uid, "2026-08-11", 900000, to=bank, kind="income", payee_raw="Salary")  # outside both sides
    with db.read_session() as s:
        r: ReportOut = report(s, uid, "2026-08-01", "2026-08-31")
        card_row: Account = s.get_one(Account, card)
        change: int = sum(signed(card_row, t) for t in s.query(TransactionEffective).filter(
            TransactionEffective.user_id == uid, TransactionEffective.transaction_date.between("2026-08-01", "2026-08-31")))
    assert r.spending_paise - r.cash_out_paise == change == 120000 + 45000 - 15000 - 100000
