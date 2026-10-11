"""The pipeline (tech-design §6.1): routed Sources become transactions, in ONE write transaction.

    parse -> deduplicate -> normalize payees -> detect transfers -> resolve status

Any step failing writes nothing: the caller's write session rolls the whole
batch back, Sources stay pending and keep their content, and the next sync
tries again. Dedup and transfer detection are slice 15 (ruling 2026-10-11):
their places in the order are marked below and records pass through unchanged.

Per routed Source this also records the template failure counters (§4.3 step
9), stores a new cache entry, sets processing_state, writes the diagnostic, and
discards content when routing said so (§3.7).

A transaction is upserted on derived_key (§3.6), so re-parsing a Source updates
its transaction in place and user edits (the overlay) stay attached. Its derived
fields are rewritten only while this Source is still primary: once a statement
has superseded the email (slice 19), the statement's values stand (§5.2).
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hisaab.accounts.repository import resolve_identifier
from hisaab.models import Account, BalanceObservation, ExtractionCache, Source, Transaction, TransactionSource
from hisaab.parsing.templates import record_outcome
from hisaab.payees.normalization import PatternRule, load_rules, match_payee
from hisaab.pipeline.routing import Routed
from hisaab.transactions.repository import derived_key
from hisaab.tunables import tunable

EMAIL_DISCRIMINATOR: str = "0"  # an email yields one transaction: its ordinal when there is no reference


@dataclass
class PipelineCounts:
    transactions_new: int = 0
    transactions_updated: int = 0
    needs_review: int = 0
    non_transactional: int = 0
    unparsed: int = 0  # Sources in state failed: they go to review as unparsed
    pending_llm: int = 0


@dataclass
class _Record:
    """One parsed transaction on its way through the stages."""
    kind: str
    amount: int
    from_account_id: str | None
    to_account_id: str | None
    unresolved_from: str | None
    unresolved_to: str | None
    payee_raw: str | None
    reference_number: str | None
    transaction_date: str
    transaction_time: str | None
    payee_id: str | None = None


def apply(session: Session, user_id: str, batch: Sequence[tuple[str, Routed]]) -> PipelineCounts:
    """batch: (source id, how it was routed), in ingest order."""
    rules: list[PatternRule] = load_rules(session)
    threshold: int = tunable(session, "large_amount_threshold_paise", user_id)
    counts: PipelineCounts = PipelineCounts()
    for source_id, routed in batch:
        source: Source = session.get_one(Source, source_id)
        if routed.interpretation is not None:
            record_outcome(session, routed.interpretation)
        if routed.cache_entry is not None and session.get(ExtractionCache, routed.cache_entry.content_hash) is None:
            session.add(routed.cache_entry)
        source.processing_state = routed.state
        source.diagnostic = json.dumps(routed.diagnostic)
        if routed.discard_content:
            source.content = None
        if routed.state == "parsed" and routed.extraction is not None:
            _derive(session, user_id, source, routed.extraction, rules, threshold, counts)
        counts.non_transactional += routed.state == "non_transactional"
        counts.unparsed += routed.state == "failed"
        counts.pending_llm += routed.state == "pending_llm"
    session.flush()
    return counts


def _derive(session: Session, user_id: str, source: Source, extraction: dict[str, Any], rules: list[PatternRule],
            threshold: int, counts: PipelineCounts) -> None:
    record: _Record
    account: Account | None
    record, account = _parse(session, user_id, extraction)
    # deduplicate: slice 15 (reference number, then account + window + amount)
    record.payee_id = match_payee(rules, record.payee_raw) if record.payee_raw else None
    # detect transfers: slice 15 (matched pair, or counterparty is one of the user's own identifiers)
    status, reason = _status(record, extraction, threshold)

    key: str = derived_key(user_id, source.ingest_seq, record.reference_number or EMAIL_DISCRIMINATOR)
    txn: Transaction | None = session.scalars(
        select(Transaction).where(Transaction.user_id == user_id, Transaction.derived_key == key)).first()
    values: dict[str, Any] = {
        "kind": record.kind, "amount": record.amount, "from_account_id": record.from_account_id,
        "to_account_id": record.to_account_id, "unresolved_from_identifier": record.unresolved_from,
        "unresolved_to_identifier": record.unresolved_to, "payee_id": record.payee_id, "payee_raw": record.payee_raw,
        "reference_number": record.reference_number, "transaction_date": record.transaction_date,
        "transaction_time": record.transaction_time, "status": status, "review_reason": reason,
    }
    if txn is None:
        txn = Transaction(user_id=user_id, derived_key=key, primary_source_id=source.id, **values)
        session.add(txn)
        session.flush()
        counts.transactions_new += 1
    elif txn.primary_source_id == source.id:
        if txn.status == "reversed":
            values.pop("status")
        for name, value in values.items():
            setattr(txn, name, value)
        counts.transactions_updated += 1
    if session.get(TransactionSource, (txn.id, source.id)) is None:
        session.add(TransactionSource(transaction_id=txn.id, source_id=source.id))
    counts.needs_review += status == "needs_review"
    if extraction.get("balance") is not None and account is not None:
        _observe(session, user_id, account.id, extraction["balance"], source)


def _parse(session: Session, user_id: str, extraction: dict[str, Any]) -> tuple[_Record, Account | None]:
    raw_account: str | None = extraction.get("account_identifier")
    account: Account | None = resolve_identifier(session, user_id, raw_account) if raw_account else None
    own: str | None = account.id if account else None
    unresolved: str | None = None if account else raw_account
    credit: bool = extraction.get("direction") == "credit"
    return _Record(
        kind="income" if credit else "expense",  # transfers and refunds: slice 15
        amount=extraction["amount"],
        from_account_id=None if credit else own, unresolved_from=None if credit else unresolved,
        to_account_id=own if credit else None, unresolved_to=unresolved if credit else None,
        payee_raw=extraction.get("payee_raw"), reference_number=extraction.get("reference_number"),
        transaction_date=extraction["date"], transaction_time=extraction.get("time"),
    ), account


def _status(record: _Record, extraction: dict[str, Any], threshold: int) -> tuple[str, str | None]:
    """§6.6: needs_review when anything about the record is uncertain; why, in words."""
    reasons: list[str] = []
    if extraction.get("missing_fields"):
        reasons.append("couldn't read: " + ", ".join(extraction["missing_fields"]))
    if extraction.get("direction") is None:
        reasons.append("couldn't tell money in from money out")
    unresolved: str | None = record.unresolved_from or record.unresolved_to
    if unresolved:
        reasons.append(f"account {unresolved} isn't one of yours yet")
    elif record.from_account_id is None and record.to_account_id is None:
        reasons.append("no account named in the email")
    if record.payee_id is None:
        reasons.append("payee not recognised" if record.payee_raw else "no payee in the email")
    if record.amount > threshold:
        reasons.append(f"amount over ₹{threshold // 100:,}")
    currency: str | None = extraction.get("currency")
    if currency not in (None, "INR"):
        reasons.append(f"currency {currency} isn't supported (INR only)")
    return ("needs_review", "; ".join(reasons)) if reasons else ("confirmed", None)


def _observe(session: Session, user_id: str, account_id: str, balance: int, source: Source) -> None:
    """A balance the email states (§10.4), once per Source: re-parsing must not duplicate it."""
    seen: BalanceObservation | None = session.scalars(select(BalanceObservation).where(
        BalanceObservation.source_id == source.id, BalanceObservation.account_id == account_id)).first()
    if seen is None:
        session.add(BalanceObservation(user_id=user_id, account_id=account_id, balance=balance,
                                       observed_at=source.received_at, source="email", source_id=source.id))
    else:
        seen.balance = balance
