"""The pipeline (tech-design §6.1): routed Sources become transactions, in ONE write transaction.

    parse -> deduplicate -> normalize payees -> detect transfers -> resolve status

Any step failing writes nothing: the caller's write session rolls the whole
batch back, Sources stay pending and keep their content, and the next sync
tries again.

A transaction is one movement of money. Each Source sees one side of it: a
debit names the account money left (the from-leg), a credit the account it
reached (the to-leg). So:

- Deduplicate (§6.2): a Source joins a transaction that already has its side.
  The reference number is identity; else the same account (hard: a looser rule
  would merge the two legs of a transfer), the same amount, dates within ±3
  days, and never two emails from one sender (card alerts carry no reference,
  and two equal purchases on one card days apart are two purchases).
- Detect transfers (§6.3, rulings 2026-10-11): a one-sided debit and a one-sided
  credit on two different own accounts, same amount, within ±5 days, become ONE
  transfer with both Sources linked; the row anchored later is absorbed into
  the earlier, so derived_key keeps the MIN(ingest_seq) anchor (§3.6). A debit
  whose counterparty is one of the user's own accounts is a transfer by itself.
  A credit into a card naming no merchant is a card bill payment: a transfer
  whose paying account is unknown until a bank debit (or the user) fills it in.
  Every run re-examines the transactions it touched plus the last 5 days of
  unmatched debits, so a debit whose credit arrives a run later is reclassified.

Kind follows from the legs. The highest-precedence Source is primary
(manual > statement > email) and writes the shared fields; any Source fills its
own side's leg when that leg is empty. Re-parsing a Source finds its transaction
through the link table and updates it in place, so user edits (the overlay) stay
attached. Per routed Source this also records the template failure counters
(§4.3 step 9), stores a new cache entry, sets processing_state, writes the
diagnostic, and discards content when routing said so (§3.7).
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from hisaab.accounts.repository import resolve_identifier
from hisaab.clock import local_today
from hisaab.models import (
    Account,
    BalanceObservation,
    ExtractionCache,
    Source,
    Transaction,
    TransactionEdit,
    TransactionSource,
)
from hisaab.parsing.templates import record_outcome
from hisaab.payees.normalization import PatternRule, load_rules, match_payee
from hisaab.pipeline.routing import Routed
from hisaab.transactions.repository import derived_key
from hisaab.tunables import tunable

EMAIL_DISCRIMINATOR: str = "0"  # an email yields one transaction: its ordinal when there is no reference
PRECEDENCE: dict[str, int] = {"email": 0, "statement": 1, "manual": 2}  # §4.2; the user's own entry ranks first
LEGS: dict[str, tuple[str, str]] = {"from": ("from_account_id", "unresolved_from_identifier"),
                                    "to": ("to_account_id", "unresolved_to_identifier")}
CARD_PAYMENT: str = "card payment received: which of your accounts paid it?"


@dataclass
class PipelineCounts:
    transactions_new: int = 0
    transactions_updated: int = 0
    duplicates: int = 0  # Sources that joined a transaction another email had already made
    needs_review: int = 0
    non_transactional: int = 0
    unparsed: int = 0  # Sources in state failed: they go to review as unparsed
    pending_llm: int = 0


@dataclass
class _Record:
    """What one Source says about its side of a movement of money."""
    side: str  # "from": money left the account it names; "to": money reached it
    amount: int
    account_id: str | None
    unresolved: str | None  # the identifier, when it names no account of the user's
    other_account_id: str | None  # the counterparty, when it is another of the user's own accounts
    payee_raw: str | None
    payee_id: str | None
    reference_number: str | None
    transaction_date: str
    transaction_time: str | None
    sender: str | None


@dataclass
class _Run:
    session: Session
    user_id: str
    rules: list[PatternRule]
    threshold: int
    tolerance: int
    dedup_days: int
    transfer_days: int
    counts: PipelineCounts = field(default_factory=PipelineCounts)
    touched: dict[str, Transaction] = field(default_factory=dict)  # status is resolved for these
    placed: set[str] = field(default_factory=set)  # transactions this run's Sources landed on
    created: set[str] = field(default_factory=set)
    reparsed: set[str] = field(default_factory=set)
    deleted: set[str] = field(default_factory=set)
    doubts: dict[str, str] = field(default_factory=dict)  # transaction id -> an ambiguous transfer match


def apply(session: Session, user_id: str, batch: Sequence[tuple[str, Routed]]) -> PipelineCounts:
    """batch: (source id, how it was routed), in ingest order."""
    run: _Run = _Run(session, user_id, load_rules(session), tunable(session, "large_amount_threshold_paise", user_id),
                     tunable(session, "dedup_amount_tolerance_paise", user_id),
                     tunable(session, "dedup_date_window_days", user_id),
                     tunable(session, "transfer_match_window_days", user_id))
    for source_id, routed in batch:
        source: Source = session.get_one(Source, source_id)
        if routed.interpretation is not None:
            record_outcome(session, routed.interpretation)
        if routed.cache_entry is not None and session.get(ExtractionCache, routed.cache_entry.content_hash) is None:
            session.add(routed.cache_entry)
        source.processing_state = routed.state
        diagnostic: dict[str, Any] = dict(routed.diagnostic)
        if routed.state == "parsed" and routed.extraction is not None:
            diagnostic["review"] = _parse_reasons(routed.extraction)
        source.diagnostic = json.dumps(diagnostic)
        if routed.discard_content:
            source.content = None
        if routed.state == "parsed" and routed.extraction is not None:
            _place(run, source, routed.extraction, diagnostic.get("sender"))
        run.counts.non_transactional += routed.state == "non_transactional"
        run.counts.unparsed += routed.state == "failed"
        run.counts.pending_llm += routed.state == "pending_llm"
    session.flush()

    for txn in _to_examine(run):
        if txn.id not in run.deleted:
            doubt: str | None = _pair(run, txn)
            if doubt is not None:
                run.doubts[txn.id] = doubt
                run.touched[txn.id] = txn
    for txn in run.touched.values():
        status, txn.review_reason = _status(run, txn)
        if txn.status != "reversed":
            txn.status = status
        run.counts.needs_review += txn.id in run.placed and status == "needs_review"
    run.counts.transactions_new = len(run.created - run.deleted)
    run.counts.transactions_updated = len(run.reparsed - run.created - run.deleted)
    session.flush()
    return run.counts


# --- parse and deduplicate ----------------------------------------------------------------------------

def _place(run: _Run, source: Source, extraction: dict[str, Any], sender: str | None) -> None:
    session: Session = run.session
    record: _Record
    account: Account | None
    record, account = _parse(session, run.user_id, extraction, sender)
    record.payee_id = match_payee(run.rules, record.payee_raw) if record.payee_raw else None

    txn: Transaction | None = session.scalars(
        select(Transaction).join(TransactionSource, TransactionSource.transaction_id == Transaction.id)
        .where(TransactionSource.source_id == source.id, Transaction.user_id == run.user_id)).first()
    if txn is not None:  # a re-parse: the transaction this Source already belongs to
        run.reparsed.add(txn.id)
    else:
        txn = _duplicate_of(run, record)
        run.counts.duplicates += txn is not None
    if txn is None:
        key: str = derived_key(run.user_id, source.ingest_seq, record.reference_number or EMAIL_DISCRIMINATOR)
        txn = Transaction(user_id=run.user_id, derived_key=key, primary_source_id=source.id, status="confirmed")
        _write(txn, record, primary=True)
        txn.kind = _kind(session, txn, "expense" if record.side == "from" else "income")
        session.add(txn)
        session.flush()
        run.created.add(txn.id)
    else:
        current: Source = session.get_one(Source, txn.primary_source_id)
        if PRECEDENCE[source.kind] > PRECEDENCE[current.kind]:  # supersession: a statement over an email
            txn.primary_source_id = source.id
        _write(txn, record, primary=txn.primary_source_id == source.id)
        txn.kind = _kind(session, txn, txn.kind)
    if session.get(TransactionSource, (txn.id, source.id)) is None:
        session.add(TransactionSource(transaction_id=txn.id, source_id=source.id))
    run.touched[txn.id] = txn
    run.placed.add(txn.id)
    if extraction.get("balance") is not None and account is not None:
        _observe(session, run.user_id, account.id, extraction["balance"], source)


def _parse(session: Session, user_id: str, extraction: dict[str, Any],
           sender: str | None) -> tuple[_Record, Account | None]:
    raw_account: str | None = extraction.get("account_identifier")
    account: Account | None = resolve_identifier(session, user_id, raw_account) if raw_account else None
    raw_other: str | None = extraction.get("counterparty_identifier")
    other: Account | None = resolve_identifier(session, user_id, raw_other) if raw_other and account else None
    return _Record(
        side="to" if extraction.get("direction") == "credit" else "from",
        amount=extraction["amount"],
        account_id=account.id if account else None, unresolved=None if account else raw_account,
        other_account_id=other.id if other and account and other.id != account.id else None,
        payee_raw=extraction.get("payee_raw"), payee_id=None, reference_number=extraction.get("reference_number"),
        transaction_date=extraction["date"], transaction_time=extraction.get("time"), sender=sender,
    ), account


def _has_side(side: str) -> ColumnElement[bool]:
    """Transactions that already have this side of the movement."""
    leg, unresolved = LEGS[side]
    plain: ColumnElement[bool] = (Transaction.kind == "expense" if side == "from"
                                  else Transaction.kind.in_(("income", "refund")))
    return or_(getattr(Transaction, leg).is_not(None), getattr(Transaction, unresolved).is_not(None), plain)


def _duplicate_of(run: _Run, record: _Record) -> Transaction | None:
    """§6.2: the transaction this record is another sighting of, strongest signal first."""
    mine: Any = select(Transaction).where(Transaction.user_id == run.user_id, _has_side(record.side))
    if record.reference_number is not None:
        same_ref: Transaction | None = run.session.scalars(
            mine.where(Transaction.reference_number == record.reference_number)).first()
        if same_ref is not None:
            return same_ref
    if record.account_id is None and record.unresolved is None:
        return None  # no account to be equal to: account equality is hard
    leg, unresolved = LEGS[record.side]
    day: date = date.fromisoformat(record.transaction_date)
    near: Any = mine.where(
        getattr(Transaction, leg) == record.account_id if record.account_id
        else getattr(Transaction, unresolved) == record.unresolved,
        Transaction.amount.between(record.amount - run.tolerance, record.amount + run.tolerance),
        Transaction.transaction_date.between((day - timedelta(days=run.dedup_days)).isoformat(),
                                             (day + timedelta(days=run.dedup_days)).isoformat()))
    if record.reference_number is not None:
        near = near.where(Transaction.reference_number.is_(None))  # two different references: two transactions
    candidates: list[Transaction] = [t for t in run.session.scalars(near)
                                     if record.sender is None or record.sender not in _senders(run.session, t)]
    candidates.sort(key=lambda t: abs((date.fromisoformat(t.transaction_date) - day).days))
    return candidates[0] if candidates else None


def _senders(session: Session, txn: Transaction) -> set[str]:
    diagnostics: Sequence[str | None] = session.scalars(
        select(Source.diagnostic).join(TransactionSource, TransactionSource.source_id == Source.id)
        .where(TransactionSource.transaction_id == txn.id)).all()
    return {json.loads(d).get("sender") for d in diagnostics if d} - {None}


def _write(txn: Transaction, record: _Record, *, primary: bool) -> None:
    """The primary Source writes the shared fields; any Source fills its own side when that side is empty."""
    leg, unresolved = LEGS[record.side]
    other_leg, other_unresolved = LEGS["to" if record.side == "from" else "from"]
    if primary:
        txn.amount, txn.transaction_date, txn.transaction_time = (record.amount, record.transaction_date,
                                                                  record.transaction_time)
        txn.payee_raw, txn.payee_id, txn.reference_number = record.payee_raw, record.payee_id, record.reference_number
    if primary or (getattr(txn, leg) is None and getattr(txn, unresolved) is None):
        setattr(txn, leg, record.account_id)
        setattr(txn, unresolved, record.unresolved)
    if record.other_account_id is not None and getattr(txn, other_unresolved) is None \
            and getattr(txn, other_leg) in (None, record.other_account_id):
        setattr(txn, other_leg, record.other_account_id)  # §6.3 condition 2: the other side is the user's too


def _kind(session: Session, txn: Transaction, fallback: str) -> str:
    """Kind follows from the legs (PRD §3.2); `fallback` when the email named no account at all."""
    if txn.from_account_id and txn.to_account_id and txn.from_account_id != txn.to_account_id:
        return "transfer"
    if txn.from_account_id or txn.unresolved_from_identifier:
        return "expense"
    if txn.to_account_id:
        if session.get_one(Account, txn.to_account_id).type == "credit_card":
            return "refund" if txn.payee_raw else "transfer"  # no merchant: a card bill payment, payer unknown
        return "income"
    if txn.unresolved_to_identifier:
        return "income"
    return fallback


# --- detect transfers ---------------------------------------------------------------------------------

def _to_examine(run: _Run) -> list[Transaction]:
    """This run's transactions, plus every unmatched debit of the last N days (the rolling window, §6.3)."""
    since: str = (local_today() - timedelta(days=run.transfer_days)).isoformat()
    recent: Sequence[Transaction] = run.session.scalars(select(Transaction).where(
        Transaction.user_id == run.user_id, Transaction.kind == "expense", Transaction.from_account_id.is_not(None),
        Transaction.to_account_id.is_(None), Transaction.unresolved_to_identifier.is_(None),
        Transaction.transaction_date >= since).order_by(Transaction.transaction_date)).all()
    return list({t.id: t for t in [*run.touched.values(), *recent]}.values())


def _partners(run: _Run, txn: Transaction) -> list[Transaction]:
    """The other side of a one-sided movement: same amount, ±N days, a different own account."""
    debit: bool = (txn.kind == "expense" and txn.from_account_id is not None and txn.to_account_id is None
                   and txn.unresolved_to_identifier is None)
    credit: bool = (txn.kind in ("income", "transfer") and txn.to_account_id is not None
                    and txn.from_account_id is None and txn.unresolved_from_identifier is None)
    if not (debit or credit):
        return []
    day: date = date.fromisoformat(txn.transaction_date)
    query: Any = select(Transaction).where(
        Transaction.user_id == run.user_id, Transaction.id != txn.id, Transaction.amount == txn.amount,
        Transaction.transaction_date.between((day - timedelta(days=run.transfer_days)).isoformat(),
                                             (day + timedelta(days=run.transfer_days)).isoformat()))
    if debit:
        query = query.where(Transaction.kind.in_(("income", "transfer")), Transaction.to_account_id.is_not(None),
                            Transaction.to_account_id != txn.from_account_id, Transaction.from_account_id.is_(None),
                            Transaction.unresolved_from_identifier.is_(None))
    else:
        query = query.where(Transaction.kind == "expense", Transaction.from_account_id.is_not(None),
                            Transaction.from_account_id != txn.to_account_id, Transaction.to_account_id.is_(None),
                            Transaction.unresolved_to_identifier.is_(None))
    found: list[Transaction] = [t for t in run.session.scalars(query) if t.id not in run.deleted]
    same_ref: list[Transaction] = [t for t in found if txn.reference_number and t.reference_number == txn.reference_number]
    return same_ref if len(same_ref) == 1 else found


def _pair(run: _Run, txn: Transaction) -> str | None:
    """§6.3 condition 1. Joins txn with its one partner; returns why not, when the match is ambiguous."""
    partners: list[Transaction] = _partners(run, txn)
    if not partners:
        return None
    if len(partners) > 1 or len(_partners(run, partners[0])) > 1:
        return f"possible transfer: {max(len(partners), len(_partners(run, partners[0])))} matching transactions"
    survivor, absorbed = sorted((txn, partners[0]), key=lambda t: _anchor(run.session, t))
    if run.session.scalar(select(func.count()).select_from(TransactionEdit)
                          .where(TransactionEdit.transaction_id == absorbed.id)):
        return "possible transfer: matches a transaction you've edited"  # absorbing would delete the edits' row
    _absorb(run, survivor, absorbed)
    return None


def _anchor(session: Session, txn: Transaction) -> int:
    seq: int = session.scalars(select(func.min(Source.ingest_seq)).join(
        TransactionSource, TransactionSource.source_id == Source.id)
        .where(TransactionSource.transaction_id == txn.id)).one()
    return seq


def _absorb(run: _Run, survivor: Transaction, absorbed: Transaction) -> None:
    session: Session = run.session
    for leg, unresolved in LEGS.values():
        if getattr(survivor, leg) is None and getattr(survivor, unresolved) is None:
            setattr(survivor, leg, getattr(absorbed, leg))
    if PRECEDENCE[session.get_one(Source, absorbed.primary_source_id).kind] > \
            PRECEDENCE[session.get_one(Source, survivor.primary_source_id).kind]:
        survivor.primary_source_id = absorbed.primary_source_id
        for name in ("amount", "transaction_date", "transaction_time", "payee_raw", "payee_id", "reference_number"):
            setattr(survivor, name, getattr(absorbed, name))
    links: list[TransactionSource] = list(session.scalars(
        select(TransactionSource).where(TransactionSource.transaction_id == absorbed.id)))
    for link in links:
        session.delete(link)
    session.flush()
    session.add_all(TransactionSource(transaction_id=survivor.id, source_id=link.source_id) for link in links)
    session.delete(absorbed)
    survivor.kind = _kind(session, survivor, survivor.kind)
    session.flush()
    run.deleted.add(absorbed.id)
    run.touched.pop(absorbed.id, None)
    run.touched[survivor.id] = survivor
    if absorbed.id in run.placed:
        run.placed.add(survivor.id)


# --- resolve status -----------------------------------------------------------------------------------

def _parse_reasons(extraction: dict[str, Any]) -> list[str]:
    """What the email itself left uncertain. Kept in the Source's diagnostic, so a later run can
    resolve the transaction's status from its primary Source after the content is gone."""
    reasons: list[str] = []
    if extraction.get("missing_fields"):
        reasons.append("couldn't read: " + ", ".join(extraction["missing_fields"]))
    if extraction.get("direction") is None:
        reasons.append("couldn't tell money in from money out")
    currency: str | None = extraction.get("currency")
    if currency not in (None, "INR"):
        reasons.append(f"currency {currency} isn't supported (INR only)")
    return reasons


def _status(run: _Run, txn: Transaction) -> tuple[str, str | None]:
    """§6.6: needs_review when anything about the transaction is uncertain; why, in words."""
    primary: Source = run.session.get_one(Source, txn.primary_source_id)
    reasons: list[str] = list(json.loads(primary.diagnostic or "{}").get("review", []))
    transfer: bool = txn.kind == "transfer"
    if transfer and txn.from_account_id is None:
        reasons.append(CARD_PAYMENT)
    unresolved: str | None = txn.unresolved_from_identifier or txn.unresolved_to_identifier
    if unresolved:
        reasons.append(f"account {unresolved} isn't one of yours yet")
    elif txn.from_account_id is None and txn.to_account_id is None:
        reasons.append("no account named in the email")
    if not transfer and txn.payee_id is None:
        reasons.append("payee not recognised" if txn.payee_raw else "no payee in the email")
    if txn.amount > run.threshold:
        reasons.append(f"amount over ₹{run.threshold // 100:,}")
    if primary.kind == "statement":
        reasons.extend(_contradictions(run.session, txn))
    if txn.id in run.doubts:
        reasons.append(run.doubts[txn.id])
    return ("needs_review", "; ".join(reasons)) if reasons else ("confirmed", None)


def _contradictions(session: Session, txn: Transaction) -> list[str]:
    """§6.6: a statement disagreeing with a value the user set. The edit still wins; the user decides."""
    said: dict[str, str] = {"amount": str(txn.amount), "transaction_date": txn.transaction_date}
    edits: Sequence[TransactionEdit] = session.scalars(select(TransactionEdit).where(
        TransactionEdit.transaction_id == txn.id, TransactionEdit.field.in_(said))).all()
    return [f"the statement disagrees with your {'date' if e.field == 'transaction_date' else 'amount'}"
            for e in edits if e.new_value != said[e.field]]


def _observe(session: Session, user_id: str, account_id: str, balance: int, source: Source) -> None:
    """A balance the email states (§10.4), once per Source: re-parsing must not duplicate it."""
    seen: BalanceObservation | None = session.scalars(select(BalanceObservation).where(
        BalanceObservation.source_id == source.id, BalanceObservation.account_id == account_id)).first()
    if seen is None:
        session.add(BalanceObservation(user_id=user_id, account_id=account_id, balance=balance,
                                       observed_at=source.received_at, source="email", source_id=source.id))
    else:
        seen.balance = balance
