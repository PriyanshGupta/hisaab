"""Confirm-and-merge (tech-design §7 step 6, §6.2, amendment 11; rulings 2026-10-19).

One write transaction: a content-less statement Source; every row matched again
(one transaction per row at most); matches superseded on amount but never on an
edited field or the payee; new rows inserted; transfers paired; statuses
resolved; the closing balance observed; the staging row deleted.
"""

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Connection, func, select

from hisaab.accounts import repository as accounts
from hisaab.db import Database
from hisaab.jobs.queue import JobContext
from hisaab.models import (
    BalanceObservation,
    Source,
    StatementStaging,
    Transaction,
    TransactionSource,
    utc_now,
)
from hisaab.models.views import TransactionEffective
from hisaab.parsing.statement_interpreter import StatementRow
from hisaab.statements import merge
from hisaab.transactions import repository as txns
from hisaab.transactions.review import CARD_PAYMENT, refresh
from tests import factories as f
from tests.pipeline_helpers import BANK_EMAIL, CARD_EMAIL, contract, ingest, make_accounts

DAY: str = "2026-09-01"
NEXT: str = "2026-09-02"


@pytest.fixture
def user_id(db: Database, conn: Connection) -> str:
    uid: str = f.user(conn)["id"]
    conn.commit()
    return uid


@pytest.fixture
def acct(db: Database, user_id: str) -> dict[str, str]:
    return make_accounts(db, user_id)  # bank 1111, other_bank 2222, card 3333


def row(date: str, direction: str, amount: int, narration: str = "CORNER SHOP", *, reference: str | None = None,
        time: str | None = None, payment: bool = False) -> StatementRow:
    return StatementRow(page=1, date=date, time=time, narration=narration, reference=reference, direction=direction,
                        amount=amount, balance=None, payment=payment)


def stage_rows(db: Database, user_id: str, account_id: str | None, rows: list[StatementRow], *,
               outcome: str = "passed", closing: int | None = 500000, kind: str = "bank",
               expires_in: timedelta = timedelta(hours=24)) -> str:
    diagnostic: dict[str, Any] = {
        "outcome": outcome, "template_id": None, "template_name": "Example", "pages_processed": 1,
        "rows_extracted": len(rows), "rows_skipped": 0, "rows_unparseable": 0,
        "declared": {"opening": 100000, "closing": closing}, "computed": {"credits": 0, "debits": 0, "closing": 0},
        "gap_paise": 200000 if outcome == "failed" else 0, "unparseable_rows": [], "account_kind": kind}
    with db.write_session() as s:
        staging: StatementStaging = StatementStaging(
            user_id=user_id, account_id=account_id, filename="statement.pdf", content_hash="hash-1",
            parsed_rows=json.dumps([dataclasses.asdict(r) for r in rows]), row_dispositions="[]",
            declared_opening=100000, declared_closing=closing, validation_result=json.dumps(diagnostic),
            expires_at=utc_now(datetime.now(UTC) + expires_in))
        s.add(staging)
        s.flush()
        return staging.id


def merge_staged(db: Database, user_id: str, staging_id: str, accept_gap: bool = False) -> dict[str, int]:
    result: dict[str, int] = merge.run(JobContext(db, "merge-job", user_id,
                                                  {"staging_id": staging_id, "accept_gap": accept_gap}))
    return result


def merge_rows(db: Database, user_id: str, account_id: str, rows: list[StatementRow], **kw: Any) -> dict[str, int]:
    return merge_staged(db, user_id, stage_rows(db, user_id, account_id, rows, **kw))


def effective(db: Database, txn_id: str) -> TransactionEffective:
    with db.read_session() as s:
        t: TransactionEffective = s.scalars(select(TransactionEffective).where(TransactionEffective.id == txn_id)).one()
        s.expunge(t)
        return t


def txn_ids(db: Database, user_id: str) -> list[str]:
    with db.read_session() as s:
        return list(s.scalars(select(Transaction.id).where(Transaction.user_id == user_id)
                              .order_by(Transaction.transaction_date, Transaction.created_at)))


def statement_sources(db: Database, user_id: str) -> list[str]:
    with db.read_session() as s:
        return list(s.scalars(select(Source.id).where(Source.user_id == user_id, Source.kind == "statement")
                              .order_by(Source.ingest_seq)))


def sources_of(db: Database, txn_id: str) -> set[str]:
    with db.read_session() as s:
        return set(s.scalars(select(TransactionSource.source_id).where(TransactionSource.transaction_id == txn_id)))


def primary_kind(db: Database, txn_id: str) -> str:
    with db.read_session() as s:
        return s.get_one(Source, s.get_one(Transaction, txn_id).primary_source_id).kind


def edit(db: Database, user_id: str, txn_id: str, **changes: object) -> None:
    with db.write_session() as s:
        txns.apply_edits(s, user_id, txn_id, changes)


def test_a_match_takes_the_statement_amount_but_keeps_an_edited_field_and_the_payee(
        db: Database, user_id: str, acct: dict[str, str]) -> None:
    # a different amount can only match on the reference (§6.2: amount isn't identity); a parse error is the case
    _, [email_source] = ingest(db, user_id, (contract(amount=50000, date=DAY, payee_raw="Corner Shop",
                                                      reference_number="UTR-1"), BANK_EMAIL))
    [txn_id] = txn_ids(db, user_id)
    edit(db, user_id, txn_id, transaction_date="2026-08-31")  # the user moved it
    result: dict[str, int] = merge_rows(db, user_id, acct["bank"], [row(NEXT, "debit", 50100, "UPI-CORNERSHOP-X",
                                                                         reference="UTR-1")])
    assert (result["new"], result["matched"]) == (0, 1)
    t: TransactionEffective = effective(db, txn_id)
    assert "disagrees" not in (t.review_reason or "")  # the statement never states a transaction date (amendment 11)
    assert (t.amount, t.posted_date, t.transaction_date, t.payee_raw) == (50100, NEXT, "2026-08-31", "Corner Shop")
    with db.read_session() as s:
        assert s.get_one(Transaction, txn_id).transaction_date == DAY  # amendment 11: the email's parsed value
    assert primary_kind(db, txn_id) == "statement"
    assert sources_of(db, txn_id) == {email_source, statement_sources(db, user_id)[0]}


def test_a_statement_disagreeing_with_an_edited_amount_keeps_the_edit_and_asks(
        db: Database, user_id: str, acct: dict[str, str]) -> None:
    ingest(db, user_id, (contract(amount=50000, date=DAY), BANK_EMAIL))
    [txn_id] = txn_ids(db, user_id)
    edit(db, user_id, txn_id, amount=49000)
    merge_rows(db, user_id, acct["bank"], [row(DAY, "debit", 50000)])
    t: TransactionEffective = effective(db, txn_id)
    assert t.amount == 49000 and t.status == "needs_review"
    assert "the statement disagrees with your amount" in (t.review_reason or "")


def test_new_rows_become_transactions_dated_by_the_statement(db: Database, user_id: str, acct: dict[str, str]) -> None:
    result: dict[str, int] = merge_rows(db, user_id, acct["bank"], [
        row(DAY, "debit", 12300, "UPI-TEA"), row(NEXT, "credit", 900000, "SALARY", reference="R-1", time="10:00:00")])
    assert (result["new"], result["matched"]) == (2, 0)
    tea, salary = (effective(db, i) for i in txn_ids(db, user_id))
    assert (tea.kind, tea.from_account_id, tea.transaction_date, tea.posted_date, tea.payee_raw) == (
        "expense", acct["bank"], DAY, DAY, "UPI-TEA")
    assert (salary.kind, salary.to_account_id, salary.reference_number, salary.transaction_time) == (
        "income", acct["bank"], "R-1", "10:00:00")
    assert primary_kind(db, tea.id) == "statement"


def test_merging_the_same_statement_again_supersedes_and_adds_nothing(
        db: Database, user_id: str, acct: dict[str, str]) -> None:
    rows: list[StatementRow] = [row(DAY, "debit", 12300, "UPI-TEA"), row(NEXT, "debit", 4500, "UPI-BUS")]
    merge_rows(db, user_id, acct["bank"], rows)
    first: list[str] = txn_ids(db, user_id)
    again: dict[str, int] = merge_rows(db, user_id, acct["bank"], rows)
    assert (again["new"], again["matched"]) == (0, 2) and txn_ids(db, user_id) == first
    second_source: str = statement_sources(db, user_id)[1]
    with db.read_session() as s:
        assert {s.get_one(Transaction, i).primary_source_id for i in first} == {second_source}


def test_two_identical_rows_stay_two_transactions(db: Database, user_id: str, acct: dict[str, str]) -> None:
    ingest(db, user_id, (contract(amount=10000, date=DAY), BANK_EMAIL))
    result: dict[str, int] = merge_rows(db, user_id, acct["bank"], [row(DAY, "debit", 10000), row(DAY, "debit", 10000)])
    assert (result["matched"], result["new"]) == (1, 1) and len(txn_ids(db, user_id)) == 2
    assert merge_rows(db, user_id, acct["bank"], [row(DAY, "debit", 10000), row(DAY, "debit", 10000)])["new"] == 0


def test_an_email_arriving_after_the_preview_is_matched_at_merge(
        db: Database, user_id: str, acct: dict[str, str]) -> None:
    staging_id: str = stage_rows(db, user_id, acct["bank"], [row(DAY, "debit", 10000)])
    ingest(db, user_id, (contract(amount=10000, date=DAY), BANK_EMAIL))  # a sync ran while the preview was open
    assert merge_staged(db, user_id, staging_id)["matched"] == 1 and len(txn_ids(db, user_id)) == 1


def test_a_card_payment_row_pairs_with_the_bank_debit_into_one_transfer(
        db: Database, user_id: str, acct: dict[str, str]) -> None:
    ingest(db, user_id, (contract(amount=300000, date=DAY, payee_raw="BILL PAY APP"), BANK_EMAIL))
    merge_rows(db, user_id, acct["card"], [row(NEXT, "credit", 300000, "PAYMENT RECEIVED", payment=True)],
               kind="credit_card")
    [txn_id] = txn_ids(db, user_id)
    t: TransactionEffective = effective(db, txn_id)
    assert (t.kind, t.from_account_id, t.to_account_id) == ("transfer", acct["bank"], acct["card"])


def test_an_unpaired_card_payment_row_asks_which_account_paid(db: Database, user_id: str, acct: dict[str, str]) -> None:
    merge_rows(db, user_id, acct["card"], [row(DAY, "credit", 300000, "PAYMENT RECEIVED", payment=True),
                                           row(DAY, "credit", 5000, "SHOP REFUND")], kind="credit_card")
    payment, refund = (effective(db, i) for i in txn_ids(db, user_id))
    if payment.amount != 300000:
        payment, refund = refund, payment
    assert (payment.kind, payment.from_account_id, payment.payee_raw) == ("transfer", None, None)
    assert CARD_PAYMENT in (payment.review_reason or "")
    assert refund.kind == "refund"


def test_a_card_statement_matches_the_email_purchase(db: Database, user_id: str, acct: dict[str, str]) -> None:
    ingest(db, user_id, (contract(amount=75000, date=DAY, account_identifier="3333"), CARD_EMAIL))
    assert merge_rows(db, user_id, acct["card"], [row(DAY, "debit", 75000)], kind="credit_card")["matched"] == 1


def test_the_closing_balance_is_observed_once_per_statement_date(
        db: Database, user_id: str, acct: dict[str, str]) -> None:
    rows: list[StatementRow] = [row(DAY, "debit", 100), row(NEXT, "debit", 200)]
    merge_rows(db, user_id, acct["bank"], rows, closing=500000)
    merge_rows(db, user_id, acct["bank"], rows, closing=500000)
    with db.read_session() as s:
        seen: list[BalanceObservation] = list(s.scalars(select(BalanceObservation)))
        assert len(seen) == 1
        o: BalanceObservation = seen[0]
        assert (o.account_id, o.balance, o.source, o.source_id) == (
            acct["bank"], 500000, "statement", statement_sources(db, user_id)[1])
        assert o.observed_at == "2026-09-02T18:29:59.000Z"  # the end of the last row's day in India


def test_no_closing_balance_no_observation(db: Database, user_id: str, acct: dict[str, str]) -> None:
    merge_rows(db, user_id, acct["bank"], [row(DAY, "debit", 100)], closing=None, outcome="unavailable")
    with db.read_session() as s:
        assert s.scalar(select(func.count()).select_from(BalanceObservation)) == 0


def test_the_merge_keeps_no_content_and_deletes_the_staging_row(
        db: Database, user_id: str, acct: dict[str, str]) -> None:
    staging_id: str = stage_rows(db, user_id, acct["bank"], [row(DAY, "debit", 100)])
    merge_staged(db, user_id, staging_id)
    with db.read_session() as s:
        assert s.get(StatementStaging, staging_id) is None
        source: Source = s.get_one(Source, statement_sources(db, user_id)[0])
        assert source.content is None and source.processing_state == "parsed"
        assert json.loads(source.diagnostic or "{}")["outcome"] == "passed"


def test_a_failed_check_refuses_unless_accepted_and_then_records_the_gap(
        db: Database, user_id: str, acct: dict[str, str]) -> None:
    staging_id: str = stage_rows(db, user_id, acct["bank"], [row(DAY, "debit", 100)], outcome="failed")
    with pytest.raises(merge.MergeRefused, match="don't add up"):
        merge_staged(db, user_id, staging_id)
    assert txn_ids(db, user_id) == []
    merge_staged(db, user_id, staging_id, accept_gap=True)
    with db.read_session() as s:
        diagnostic: dict[str, Any] = json.loads(s.get_one(Source, statement_sources(db, user_id)[0]).diagnostic or "")
    assert diagnostic["accepted_gap_paise"] == 200000 and len(txn_ids(db, user_id)) == 1


def test_an_unbound_or_archived_account_refuses_and_writes_nothing(
        db: Database, user_id: str, acct: dict[str, str]) -> None:
    unbound: str = stage_rows(db, user_id, None, [row(DAY, "debit", 100)])
    with pytest.raises(merge.MergeRefused, match="Pick an active account"):
        merge_staged(db, user_id, unbound)
    staging_id: str = stage_rows(db, user_id, acct["bank"], [row(DAY, "debit", 100)])
    with db.write_session() as s:
        accounts.archive_account(s, user_id, acct["bank"])
    with pytest.raises(merge.MergeRefused, match="Pick an active account"):
        merge_staged(db, user_id, staging_id)
    assert txn_ids(db, user_id) == [] and statement_sources(db, user_id) == []


def test_an_expired_or_foreign_preview_refuses(db: Database, conn: Connection, user_id: str,
                                               acct: dict[str, str]) -> None:
    expired: str = stage_rows(db, user_id, acct["bank"], [row(DAY, "debit", 100)], expires_in=timedelta(seconds=-1))
    with pytest.raises(merge.MergeRefused, match="expired"):
        merge_staged(db, user_id, expired)
    stranger: str = f.user(conn)["id"]
    conn.commit()
    live: str = stage_rows(db, user_id, acct["bank"], [row(DAY, "debit", 100)])
    with pytest.raises(merge.MergeRefused, match="expired"):
        merge_staged(db, stranger, live)


# --- found in the whole-branch review ---------------------------------------------------------------

def test_a_card_payment_pairing_with_an_older_email_keeps_its_date_and_payee(
        db: Database, user_id: str, acct: dict[str, str]) -> None:
    ingest(db, user_id, (contract(amount=300000, date=DAY, payee_raw="BILL PAY APP"), BANK_EMAIL))
    merge_rows(db, user_id, acct["card"], [row("2026-09-03", "credit", 300000, "PAYMENT RECEIVED", payment=True)],
               kind="credit_card")
    [txn_id] = txn_ids(db, user_id)
    t: TransactionEffective = effective(db, txn_id)
    assert (t.kind, t.transaction_date, t.posted_date, t.payee_raw) == ("transfer", DAY, "2026-09-03", "BILL PAY APP")
    assert primary_kind(db, txn_id) == "statement"


def test_a_reference_repeated_in_another_month_is_another_transaction(
        db: Database, user_id: str, acct: dict[str, str]) -> None:
    merge_rows(db, user_id, acct["bank"], [row("2026-08-05", "debit", 10000, "ATM", reference="000000")])
    result: dict[str, int] = merge_rows(db, user_id, acct["bank"],
                                        [row("2026-09-07", "debit", 250000, "ATM", reference="000000")])
    assert (result["new"], result["matched"]) == (1, 0)
    assert sorted(effective(db, i).amount for i in txn_ids(db, user_id)) == [10000, 250000]


def test_a_reference_on_another_account_is_not_this_statements(
        db: Database, user_id: str, acct: dict[str, str]) -> None:
    ingest(db, user_id, (contract(amount=10000, date=DAY, account_identifier="2222", reference_number="123456"),
                         BANK_EMAIL))
    result: dict[str, int] = merge_rows(db, user_id, acct["bank"], [row(DAY, "debit", 99900, reference="123456")])
    assert (result["new"], result["matched"]) == (1, 0)


def test_a_zero_padded_statement_reference_matches_the_emails(db: Database, user_id: str, acct: dict[str, str]) -> None:
    ingest(db, user_id, (contract(amount=10000, date=DAY, reference_number="412345678901"), BANK_EMAIL))
    result: dict[str, int] = merge_rows(db, user_id, acct["bank"],
                                        [row(NEXT, "debit", 10000, "UPI-SHOP", reference="0000412345678901")])
    assert (result["new"], result["matched"]) == (0, 1) and len(txn_ids(db, user_id)) == 1


def test_a_reference_repeated_inside_one_statement_identifies_nothing(
        db: Database, user_id: str, acct: dict[str, str]) -> None:
    ingest(db, user_id, (contract(amount=10000, date=DAY, reference_number="777"), BANK_EMAIL))
    result: dict[str, int] = merge_rows(db, user_id, acct["bank"], [
        row(DAY, "debit", 55500, reference="777"), row(NEXT, "debit", 66600, reference="777")])
    assert (result["new"], result["matched"]) == (2, 0)


def test_an_amount_edit_made_after_the_statement_is_the_users_answer_not_a_disagreement(
        db: Database, user_id: str, acct: dict[str, str]) -> None:
    merge_rows(db, user_id, acct["bank"], [row(DAY, "debit", 10000)])
    [txn_id] = txn_ids(db, user_id)
    edit(db, user_id, txn_id, amount=9000)
    with db.write_session() as s:
        refresh(s, user_id, [txn_id])
    assert "disagrees" not in (effective(db, txn_id).review_reason or "")
