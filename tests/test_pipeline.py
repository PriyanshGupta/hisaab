"""The pipeline (tech-design §6.1): routed Sources become transactions in one write transaction."""

import json
from collections.abc import Callable
from typing import Any, TypeAlias

import pytest
from sqlalchemy import Connection, func, select

from hisaab.accounts import repository as accounts
from hisaab.db import Database
from hisaab.models import (
    BalanceObservation,
    ExtractionCache,
    NormalizationPattern,
    ParserTemplate,
    Source,
    Transaction,
    TransactionSource,
)
from hisaab.models.views import TransactionEffective
from hisaab.payees import repository as payees
from hisaab.pipeline import derive
from hisaab.pipeline.derive import PipelineCounts, apply
from hisaab.pipeline.routing import Routed, route
from hisaab.transactions import repository as txns
from tests import factories as f
from tests.claude_fake import MARKETING, TRANSACTION
from tests.gmail_fake import make_eml
from tests.pipeline_helpers import DEBIT_EML, NOTICE_EML, UNKNOWN_EML, add_source, install_templates
from tests.test_routing import MODEL, Llm


@pytest.fixture
def user_id(db: Database, conn: Connection) -> str:
    uid: str = f.user(conn)["id"]
    conn.commit()
    install_templates(db)
    return uid


@pytest.fixture
def bank(db: Database, user_id: str) -> str:
    with db.write_session() as s:
        return accounts.create_account(s, user_id, type="bank", display_name="Savings", opening_balance=0,
                                       opening_balance_date="2026-07-01", last4="1111").id


@pytest.fixture
def known_payee(db: Database) -> str:
    with db.write_session() as s:
        payee_id: str = payees.create_payee(s, "Corner Shop").id
        s.add(NormalizationPattern(match_type="contains", pattern="corner shop", payee_id=payee_id, origin="seed"))
        return payee_id


Process: TypeAlias = Callable[..., PipelineCounts]


@pytest.fixture
def process(db: Database, user_id: str) -> Process:
    def run(*contents: bytes, llm: Llm | None = None) -> PipelineCounts:
        with db.write_session() as s:
            sources: list[str] = [add_source(s, user_id, c, f"m{i}-{len(c)}").id for i, c in enumerate(contents)]
        return reprocess(db, user_id, sources, llm or Llm())
    return run


def reprocess(db: Database, user_id: str, source_ids: list[str], llm: Llm) -> PipelineCounts:
    with db.read_session() as s:
        batch: list[tuple[str, Routed]] = [(i, route(s, s.get_one(Source, i), llm, MODEL)) for i in source_ids]
    with db.write_session() as s:
        return apply(s, user_id, batch)


def only_transaction(db: Database) -> Transaction:
    with db.read_session() as s:
        txn: Transaction = s.scalars(select(Transaction)).one()
        s.expunge(txn)
        return txn


def source_row(db: Database) -> Source:
    with db.read_session() as s:
        src: Source = s.scalars(select(Source)).one()
        s.expunge(src)
        return src


def test_a_debit_alert_becomes_an_expense_from_the_account(db: Database, process: Process, bank: str,
                                                           known_payee: str, user_id: str) -> None:
    counts: PipelineCounts = process(DEBIT_EML)
    txn: Transaction = only_transaction(db)
    src: Source = source_row(db)
    assert (txn.kind, txn.amount, txn.from_account_id, txn.to_account_id) == ("expense", 123450, bank, None)
    assert (txn.payee_raw, txn.payee_id, txn.reference_number, txn.transaction_date) == \
        ("Corner Shop", known_payee, "9988776655", "2026-10-05")
    assert (txn.status, txn.review_reason, txn.primary_source_id) == ("confirmed", None, src.id)
    assert txn.derived_key == txns.derived_key(user_id, src.ingest_seq, "9988776655")
    assert (src.processing_state, src.content) == ("parsed", None)  # tier 1: content discarded at once
    assert json.loads(src.diagnostic or "{}")["route"] == "tier1"
    with db.read_session() as s:
        assert s.scalars(select(TransactionSource.source_id)).all() == [src.id]
    assert (counts.transactions_new, counts.needs_review) == (1, 0)


def test_an_unknown_account_is_kept_as_an_unresolved_leg_for_review(db: Database, process: Process,
                                                                    known_payee: str) -> None:
    process(DEBIT_EML)
    txn: Transaction = only_transaction(db)
    assert (txn.from_account_id, txn.unresolved_from_identifier, txn.status) == (None, "1111", "needs_review")
    assert "account 1111" in (txn.review_reason or "")


def test_an_unrecognised_payee_goes_to_review(db: Database, process: Process, bank: str) -> None:
    process(DEBIT_EML)
    txn: Transaction = only_transaction(db)
    assert (txn.payee_id, txn.status) == (None, "needs_review") and "payee" in (txn.review_reason or "")


def test_a_large_amount_goes_to_review(db: Database, conn: Connection, process: Process, bank: str,
                                       known_payee: str, user_id: str) -> None:
    f.setting(conn, key="large_amount_threshold_paise", user_id=user_id, value="100000")  # ₹1,000 for this user
    conn.commit()
    process(DEBIT_EML)
    assert "over ₹1,000" in (only_transaction(db).review_reason or "")


def test_a_credit_is_income_into_the_account(db: Database, process: Process, bank: str) -> None:
    process(UNKNOWN_EML, llm=Llm({**TRANSACTION, "direction": "credit", "account_identifier": "XX1111"}))
    txn: Transaction = only_transaction(db)
    assert (txn.kind, txn.from_account_id, txn.to_account_id) == ("income", None, bank)


def test_missing_fields_and_foreign_currency_are_named_in_the_review_reason(db: Database, process: Process,
                                                                           bank: str) -> None:
    process(UNKNOWN_EML, llm=Llm({**TRANSACTION, "account_identifier": "1111", "currency": "USD",
                                  "missing_fields": ["payee_raw"]}))
    reason: str = only_transaction(db).review_reason or ""
    assert "couldn't read: payee_raw" in reason and "USD" in reason
    review: list[str] = json.loads(source_row(db).diagnostic or "{}")["review"]  # for later runs, without content
    assert review == ["couldn't read: payee_raw", "currency USD isn't supported (INR only)"]


def test_a_tier_2_read_is_cached_and_keeps_its_content(db: Database, process: Process) -> None:
    process(UNKNOWN_EML, llm=Llm())
    src: Source = source_row(db)
    assert (src.processing_state, src.content) == ("parsed", UNKNOWN_EML)
    with db.read_session() as s:
        cached: ExtractionCache = s.get_one(ExtractionCache, src.content_hash)
        assert (cached.outcome, cached.model, cached.tier) == ("transaction", MODEL, 2)


def test_a_notice_makes_no_transaction_and_discards_its_content(db: Database, process: Process) -> None:
    counts: PipelineCounts = process(NOTICE_EML)
    src: Source = source_row(db)
    assert (src.processing_state, src.content, counts.non_transactional) == ("non_transactional", None, 1)
    with db.read_session() as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0


def test_marketing_makes_no_transaction_and_stays_out_of_review(db: Database, process: Process) -> None:
    process(UNKNOWN_EML, llm=Llm(MARKETING))
    src: Source = source_row(db)
    assert (src.processing_state, src.content) == ("non_transactional", UNKNOWN_EML)
    with db.read_session() as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0


def test_an_unparseable_email_is_failed_with_its_content_kept(db: Database, process: Process) -> None:
    counts: PipelineCounts = process(UNKNOWN_EML, llm=Llm({**MARKETING, "outcome": "unparseable"}))
    assert (source_row(db).processing_state, source_row(db).content, counts.unparsed) == ("failed", UNKNOWN_EML, 1)


def test_reprocessing_updates_in_place_and_keeps_the_users_edit(db: Database, process: Process, bank: str,
                                                                known_payee: str, user_id: str) -> None:
    process(UNKNOWN_EML, llm=Llm({**TRANSACTION, "account_identifier": "1111"}))
    txn: Transaction = only_transaction(db)
    with db.write_session() as s:
        txns.apply_edits(s, user_id, txn.id, {"transaction_date": "2026-10-01"})
    src: Source = source_row(db)
    with db.write_session() as s:
        s.execute(ExtractionCache.__table__.delete())  # type: ignore[attr-defined]  # force a fresh read
    counts: PipelineCounts = reprocess(db, user_id, [src.id], Llm({**TRANSACTION, "account_identifier": "1111",
                                                                   "amount": 46000}))
    again: Transaction = only_transaction(db)
    assert (again.id, again.amount, counts.transactions_new, counts.transactions_updated) == (txn.id, 46000, 0, 1)
    with db.read_session() as s:
        effective: TransactionEffective = s.get_one(TransactionEffective, txn.id)
        assert (effective.amount, effective.transaction_date) == (46000, "2026-10-01")  # the edit survives


def test_a_stated_balance_is_observed_once_per_source(db: Database, process: Process, bank: str,
                                                      user_id: str) -> None:
    process(UNKNOWN_EML, llm=Llm({**TRANSACTION, "account_identifier": "1111", "balance": 9876500}))
    src: Source = source_row(db)
    reprocess(db, user_id, [src.id], Llm())  # a cache hit, same extraction
    with db.read_session() as s:
        obs: list[BalanceObservation] = list(s.scalars(select(BalanceObservation)))
        assert [(o.account_id, o.balance, o.source, o.source_id) for o in obs] == [(bank, 9876500, "email", src.id)]


def test_templates_that_all_declined_are_charged_a_failure(db: Database, process: Process) -> None:
    process(make_eml("alerts@example-bank.in", "Debit alert", "<p>A redesigned alert</p>"), llm=Llm())
    with db.read_session() as s:
        assert s.scalars(select(ParserTemplate.consecutive_failures).where(
            ParserTemplate.name == "Example debit")).one() == 1


def test_one_bad_source_rolls_back_the_whole_batch(db: Database, process: Process, bank: str,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    real: Callable[..., Any] = derive._status
    calls: list[int] = []

    def explodes_second(*args: Any, **kw: Any) -> Any:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(*args, **kw)

    monkeypatch.setattr(derive, "_status", explodes_second)
    with pytest.raises(RuntimeError, match="boom"):
        process(DEBIT_EML, make_eml("alerts@example-bank.in", "Debit alert",
                                    "<p>Rs. 5.00 debited from account ending 1111 to VPA a@b (X) on 05-10-26. Ref 1</p>"))
    with db.read_session() as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0
        assert set(s.scalars(select(Source.processing_state))) == {"pending"}
        assert all(c is not None for c in s.scalars(select(Source.content)))
