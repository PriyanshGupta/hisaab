"""Deduplication (tech-design §6.2): one movement of money, one transaction, however many emails say so.

Matching, strongest first: the reference number; else the same account, the
same amount, dates within ±3 days. Account equality is hard. Two emails from
one sender never fallback-merge: card alerts carry no reference, and two equal
purchases on one card days apart are two purchases.
"""

from typing import Any

import pytest
from sqlalchemy import Connection, select

from hisaab.db import Database
from hisaab.models import Source, Transaction, TransactionSource
from hisaab.models.views import TransactionEffective
from hisaab.pipeline.derive import PipelineCounts
from hisaab.transactions import repository as txns
from tests import factories as f
from tests.pipeline_helpers import (
    BANK_EMAIL,
    CARD_EMAIL,
    OTHER_BANK_EMAIL,
    contract,
    ingest,
    install_templates,
    make_accounts,
)


@pytest.fixture
def user_id(db: Database, conn: Connection) -> str:
    uid: str = f.user(conn)["id"]
    conn.commit()
    install_templates(db)
    return uid


@pytest.fixture
def acct(db: Database, user_id: str) -> dict[str, str]:
    return make_accounts(db, user_id)


def transactions(db: Database) -> list[dict[str, Any]]:
    with db.read_session() as s:
        rows: list[Transaction] = list(s.scalars(select(Transaction).order_by(Transaction.created_at)))
        return [{"id": t.id, "kind": t.kind, "amount": t.amount, "from": t.from_account_id, "to": t.to_account_id,
                 "primary": t.primary_source_id, "status": t.status, "reason": t.review_reason,
                 "sources": sorted(s.scalars(select(TransactionSource.source_id).where(
                     TransactionSource.transaction_id == t.id)))} for t in rows]


def test_one_purchase_in_two_emails_is_one_transaction_with_both_sources(db: Database, user_id: str,
                                                                         acct: dict[str, str]) -> None:
    counts, ids = ingest(db, user_id, (contract(account_identifier="3333"), CARD_EMAIL),
                         (contract(account_identifier="XX3333", payee_raw="CORNER SHOP"), BANK_EMAIL))
    [txn] = transactions(db)
    assert (txn["kind"], txn["from"], txn["sources"], txn["primary"]) == ("expense", acct["card"], sorted(ids), ids[0])
    assert (counts.transactions_new, counts.duplicates) == (1, 1)


def test_the_same_reference_is_one_transaction_even_across_runs(db: Database, user_id: str,
                                                                acct: dict[str, str]) -> None:
    _, first = ingest(db, user_id, (contract(reference_number="R1"), BANK_EMAIL))
    counts, second = ingest(db, user_id, (contract(reference_number="R1", date="2026-09-20",
                                                   account_identifier=None), CARD_EMAIL))
    [txn] = transactions(db)
    assert (txn["sources"], txn["primary"], counts.duplicates) == (sorted(first + second), first[0], 1)


def test_equal_debits_from_two_different_accounts_stay_two(db: Database, user_id: str, acct: dict[str, str]) -> None:
    """Account equality is a hard requirement: a looser rule would merge them."""
    ingest(db, user_id, (contract(account_identifier="1111"), BANK_EMAIL),
           (contract(account_identifier="2222"), OTHER_BANK_EMAIL))
    assert [(t["kind"], t["from"]) for t in transactions(db)] == [("expense", acct["bank"]),
                                                                  ("expense", acct["other_bank"])]


def test_equal_purchases_from_the_same_sender_stay_two(db: Database, user_id: str, acct: dict[str, str]) -> None:
    ingest(db, user_id, (contract(account_identifier="3333"), CARD_EMAIL))
    ingest(db, user_id, (contract(account_identifier="3333", date="2026-09-03"), CARD_EMAIL))
    assert len(transactions(db)) == 2


def test_references_that_differ_never_merge(db: Database, user_id: str, acct: dict[str, str]) -> None:
    ingest(db, user_id, (contract(reference_number="R1"), BANK_EMAIL),
           (contract(reference_number="R2"), CARD_EMAIL))
    assert len(transactions(db)) == 2


@pytest.mark.parametrize("date,merged", [("2026-09-04", True), ("2026-09-05", False)], ids=["3 days", "4 days"])
def test_the_fallback_window_is_three_days(db: Database, user_id: str, acct: dict[str, str], date: str,
                                           merged: bool) -> None:
    ingest(db, user_id, (contract(), BANK_EMAIL), (contract(date=date), CARD_EMAIL))
    assert len(transactions(db)) == (1 if merged else 2)


def test_an_email_naming_no_account_never_fallback_merges(db: Database, user_id: str, acct: dict[str, str]) -> None:
    ingest(db, user_id, (contract(), BANK_EMAIL), (contract(account_identifier=None), CARD_EMAIL))
    assert len(transactions(db)) == 2


def test_a_statement_supersedes_the_email_but_not_the_users_edit(db: Database, user_id: str,
                                                                 acct: dict[str, str]) -> None:
    _, [email] = ingest(db, user_id, (contract(reference_number="R1"), BANK_EMAIL))
    [txn] = transactions(db)
    with db.write_session() as s:
        txns.apply_edits(s, user_id, txn["id"], {"transaction_date": "2026-09-02"})
    _, [statement] = ingest(db, user_id, (contract(reference_number="R1", amount=50100, date="2026-08-31"),
                                          "statement"), kind="statement")
    [after] = transactions(db)
    assert (after["id"], after["primary"], after["amount"], after["sources"]) == \
        (txn["id"], statement, 50100, sorted([email, statement]))
    with db.read_session() as s:
        assert s.get_one(TransactionEffective, txn["id"]).transaction_date == "2026-09-02"  # the edit stands


def test_a_statement_contradicting_an_edit_goes_to_review(db: Database, user_id: str, acct: dict[str, str]) -> None:
    ingest(db, user_id, (contract(reference_number="R1", payee_raw=None), BANK_EMAIL))
    [txn] = transactions(db)
    with db.write_session() as s:
        txns.apply_edits(s, user_id, txn["id"], {"amount": 49000})
    ingest(db, user_id, (contract(reference_number="R1", amount=50100), "statement"), kind="statement")
    [after] = transactions(db)
    assert after["status"] == "needs_review" and "the statement disagrees with your amount" in after["reason"]


def test_an_email_never_takes_over_from_a_statement(db: Database, user_id: str, acct: dict[str, str]) -> None:
    _, [statement] = ingest(db, user_id, (contract(reference_number="R1"), "statement"), kind="statement")
    ingest(db, user_id, (contract(reference_number="R1", amount=99900), BANK_EMAIL))
    [txn] = transactions(db)
    assert (txn["primary"], txn["amount"]) == (statement, 50000)
    with db.read_session() as s:
        assert s.get_one(Source, statement).kind == "statement"


def test_counts_new_and_duplicates_once_each(db: Database, user_id: str, acct: dict[str, str]) -> None:
    counts: PipelineCounts = ingest(db, user_id, (contract(reference_number="R1"), BANK_EMAIL),
                                    (contract(reference_number="R1"), CARD_EMAIL),
                                    (contract(reference_number="R2"), BANK_EMAIL))[0]
    assert (counts.transactions_new, counts.duplicates, counts.transactions_updated) == (2, 1, 0)
