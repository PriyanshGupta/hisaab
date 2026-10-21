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
  A leg resolved later by a review answer pairs too (pair_resolved): a bill paid
  through an app has a bank debit naming the app and a card credit naming a card
  not yet registered, so neither side could pair at ingest.

Kind follows from the legs. The highest-precedence Source is primary
(manual > statement > email) and writes the shared fields; any Source fills its
own side's leg when that leg is empty. Re-parsing a Source finds its transaction
through the link table and updates it in place, so user edits (the overlay) stay
attached. Per routed Source this also records the template failure counters
(§4.3 step 9), stores a new cache entry, sets processing_state, writes the
diagnostic, discards content when routing said so (§3.7), and counts a model-read email
under its format's fingerprint (§4.10; once per Source, so a re-read never counts twice).
"""

import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from hisaab.accounts.repository import resolve_identifier
from hisaab.balances.checks import record_checks
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
from hisaab.parsing.statement_interpreter import StatementRow
from hisaab.parsing.templates import record_outcome
from hisaab.payees.normalization import PatternRule, load_rules, match_payee
from hisaab.pipeline.routing import Routed
from hisaab.promotion.sightings import record_sighting
from hisaab.transactions.repository import derived_key
from hisaab.transactions.review import review_reasons
from hisaab.tunables import tunable

EMAIL_DISCRIMINATOR: str = "0"  # an email yields one transaction: its ordinal when there is no reference
PRECEDENCE: dict[str, int] = {"email": 0, "statement": 1, "manual": 2}  # §4.2; the user's own entry ranks first
LEGS: dict[str, tuple[str, str]] = {"from": ("from_account_id", "unresolved_from_identifier"),
                                    "to": ("to_account_id", "unresolved_to_identifier")}


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
    from_statement: bool = False  # a statement's reference identifies only within its own account and dates


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
    merged_into: dict[str, str] = field(default_factory=dict)  # absorbed id -> its survivor's id
    doubts: dict[str, str] = field(default_factory=dict)  # transaction id -> an ambiguous transfer match
    observed: bool = False  # an email's balance was recorded: run the reconciliation check (slice 20)


def _new_run(session: Session, user_id: str) -> _Run:
    return _Run(session, user_id, load_rules(session), tunable(session, "large_amount_threshold_paise", user_id),
                tunable(session, "dedup_amount_tolerance_paise", user_id),
                tunable(session, "dedup_date_window_days", user_id),
                tunable(session, "transfer_match_window_days", user_id))


def apply(session: Session, user_id: str, batch: Sequence[tuple[str, Routed]]) -> PipelineCounts:
    """batch: (source id, how it was routed), in ingest order."""
    run: _Run = _new_run(session, user_id)
    threshold: int = tunable(session, "template_promotion_sightings")
    for source_id, routed in batch:
        source: Source = session.get_one(Source, source_id)
        counted_as: str | None = json.loads(source.diagnostic or "{}").get("fingerprint")
        if routed.interpretation is not None:
            record_outcome(session, routed.interpretation)
        if routed.cache_entry is not None and session.get(ExtractionCache, routed.cache_entry.content_hash) is None:
            session.add(routed.cache_entry)
        source.processing_state = routed.state
        diagnostic: dict[str, Any] = dict(routed.diagnostic)
        if routed.state == "parsed" and routed.extraction is not None:
            diagnostic["review"] = _parse_reasons(routed.extraction) + list(routed.review)
        source.diagnostic = json.dumps(diagnostic)
        if routed.discard_content:
            source.content = None
        if routed.fingerprint is not None and routed.extraction is not None and routed.fingerprint.value != counted_as:
            session.flush()  # the non-transactional count reads the Sources' diagnostics
            record_sighting(session, routed.fingerprint, source, routed.extraction["outcome"], threshold)
        if routed.state == "parsed" and routed.extraction is not None:
            _place(run, source, routed.extraction, diagnostic.get("sender"))
        run.counts.non_transactional += routed.state == "non_transactional"
        run.counts.unparsed += routed.state == "failed"
        run.counts.pending_llm += routed.state == "pending_llm"
    session.flush()
    _settle(run)
    if run.observed:
        record_checks(session, user_id)
    run.counts.transactions_new = len(run.created - run.deleted)
    run.counts.transactions_updated = len(run.reparsed - run.created - run.deleted)
    return run.counts


def _settle(run: _Run) -> None:
    """The pipeline's last two stages over what this run touched: detect transfers, then resolve status."""
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
    run.session.flush()


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
        txn.kind = kind_from_legs(session, txn, "expense" if record.side == "from" else "income")
        session.add(txn)
        session.flush()
        run.created.add(txn.id)
    else:
        current: Source = session.get_one(Source, txn.primary_source_id)
        if PRECEDENCE[source.kind] > PRECEDENCE[current.kind]:  # supersession: a statement over an email
            txn.primary_source_id = source.id
        _write(txn, record, primary=txn.primary_source_id == source.id)
        if txn.id in run.reparsed and current.kind == "statement" and source.kind == "email":
            _email_fields(txn, record)
        txn.kind = kind_from_legs(session, txn, txn.kind)
    if session.get(TransactionSource, (txn.id, source.id)) is None:
        session.add(TransactionSource(transaction_id=txn.id, source_id=source.id))
    run.touched[txn.id] = txn
    run.placed.add(txn.id)
    if extraction.get("balance") is not None and account is not None:
        _observe(session, run.user_id, account.id, extraction["balance"], source)
        run.observed = True


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


def _duplicate_of(run: _Run, record: _Record, exclude: Collection[str] = ()) -> Transaction | None:
    """§6.2: the transaction this record is another sighting of, strongest signal first. `exclude`: transactions
    another row of the same statement already claimed (two equal rows are two transactions)."""
    mine: Any = select(Transaction).where(Transaction.user_id == run.user_id, _has_side(record.side))
    leg, unresolved = LEGS[record.side]
    day: date = date.fromisoformat(record.transaction_date)
    window: tuple[str, str] = ((day - timedelta(days=run.dedup_days)).isoformat(),
                               (day + timedelta(days=run.dedup_days)).isoformat())
    if record.reference_number is not None:
        by_ref: Any = mine.where(_same_reference(record.reference_number))
        if record.from_statement:
            # A UTR is unique everywhere; a statement's reference column also holds cheque numbers, ATM
            # sequences and placeholders that repeat. A statement is one account (§4.7), so its reference
            # identifies only on that account, near that date (whole-branch review, 2026-10-19).
            by_ref = by_ref.where(getattr(Transaction, leg) == record.account_id,
                                  Transaction.transaction_date.between(*window))
        same_ref: list[Transaction] = [t for t in run.session.scalars(by_ref) if t.id not in exclude]
        if same_ref:
            return min(same_ref, key=lambda t: (abs((date.fromisoformat(t.transaction_date) - day).days), t.id))
    if record.account_id is None and record.unresolved is None:
        return None  # no account to be equal to: account equality is hard
    near: Any = mine.where(
        getattr(Transaction, leg) == record.account_id if record.account_id
        else getattr(Transaction, unresolved) == record.unresolved,
        Transaction.amount.between(record.amount - run.tolerance, record.amount + run.tolerance),
        Transaction.transaction_date.between(*window))
    if record.reference_number is not None:
        near = near.where(Transaction.reference_number.is_(None))  # two different references: two transactions
    candidates: list[Transaction] = [t for t in run.session.scalars(near) if t.id not in exclude
                                     and (record.sender is None or record.sender not in _senders(run.session, t))]
    candidates.sort(key=lambda t: abs((date.fromisoformat(t.transaction_date) - day).days))
    return candidates[0] if candidates else None


def _same_reference(reference: str) -> ColumnElement[bool]:
    """Equal references. All-digit ones compare without leading zeros: the HDFC savings statement prints a
    UPI reference zero-padded to 16 digits where the alert email gives the 12-digit number."""
    if reference.isdigit():
        return func.ltrim(Transaction.reference_number, "0") == reference.lstrip("0")
    return Transaction.reference_number == reference


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
    if primary:  # a re-parse that reads the other direction moves the account across: it can't pay itself
        if record.account_id is not None and getattr(txn, other_leg) == record.account_id:
            setattr(txn, other_leg, None)
        if record.unresolved is not None and getattr(txn, other_unresolved) == record.unresolved:
            setattr(txn, other_unresolved, None)
    if record.other_account_id is not None and getattr(txn, other_unresolved) is None \
            and getattr(txn, other_leg) in (None, record.other_account_id):
        setattr(txn, other_leg, record.other_account_id)  # §6.3 condition 2: the other side is the user's too


def _email_fields(txn: Transaction, record: _Record) -> None:
    """A re-parsed email under a statement-primary transaction (a re-sync, slice 24): the statement owns the
    amount and posted date; the email owns the transaction date, and its time, reference and payee when it reads
    them (amendment 11; apply_statement and _absorb keep exactly these from the email). Per field, as §5.2 asks:
    a parser fix reaches statement-matched history too."""
    txn.transaction_date = record.transaction_date
    txn.transaction_time = record.transaction_time or txn.transaction_time
    txn.reference_number = record.reference_number or txn.reference_number
    if record.payee_raw:
        txn.payee_raw, txn.payee_id = record.payee_raw, record.payee_id


def kind_from_legs(session: Session, txn: Transaction, fallback: str) -> str:
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
    """The other side of a one-sided movement: same amount, ±N days, a different own account. A card debit
    is a purchase, never the paying half of a transfer: money can't leave a card for one of your accounts,
    and pairing it with a same-amount credit (a friend repaying you) would erase both (user, 2026-10-19)."""
    debit: bool = (txn.kind == "expense" and txn.from_account_id is not None and txn.to_account_id is None
                   and txn.unresolved_to_identifier is None and not _is_card(run.session, txn.from_account_id))
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
                            Transaction.unresolved_to_identifier.is_(None),
                            Transaction.from_account_id.not_in(select(Account.id).where(Account.type == "credit_card")))
    found: list[Transaction] = [t for t in run.session.scalars(query) if t.id not in run.deleted]
    same_ref: list[Transaction] = [t for t in found if txn.reference_number and t.reference_number == txn.reference_number]
    return same_ref if len(same_ref) == 1 else found


def _is_card(session: Session, account_id: str) -> bool:
    return session.get_one(Account, account_id).type == "credit_card"


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
    absorbed_kind: str = session.get_one(Source, absorbed.primary_source_id).kind
    if PRECEDENCE[absorbed_kind] > PRECEDENCE[session.get_one(Source, survivor.primary_source_id).kind]:
        survivor.primary_source_id = absorbed.primary_source_id
        if absorbed_kind == "statement":
            # As a direct statement match (apply_statement): amount and posted date from the statement; the
            # email's transaction date (amendment 11), time, reference and payee stay when it has them.
            survivor.amount = absorbed.amount
            survivor.posted_date = absorbed.posted_date or absorbed.transaction_date
            survivor.transaction_time = survivor.transaction_time or absorbed.transaction_time
            survivor.reference_number = survivor.reference_number or absorbed.reference_number
            if survivor.payee_raw is None:
                survivor.payee_raw, survivor.payee_id = absorbed.payee_raw, absorbed.payee_id
        else:
            for name in ("amount", "transaction_date", "transaction_time", "payee_raw", "payee_id", "reference_number"):
                setattr(survivor, name, getattr(absorbed, name))
    links: list[TransactionSource] = list(session.scalars(
        select(TransactionSource).where(TransactionSource.transaction_id == absorbed.id)))
    for link in links:
        session.delete(link)
    session.flush()
    session.add_all(TransactionSource(transaction_id=survivor.id, source_id=link.source_id) for link in links)
    session.delete(absorbed)
    survivor.kind = kind_from_legs(session, survivor, survivor.kind)
    session.flush()
    run.deleted.add(absorbed.id)
    run.merged_into[absorbed.id] = survivor.id
    run.touched.pop(absorbed.id, None)
    run.touched[survivor.id] = survivor
    if absorbed.id in run.placed:
        run.placed.add(survivor.id)


def pair_resolved(session: Session, user_id: str, txn_ids: Sequence[str]) -> dict[str, str]:
    """§6.3 for transactions whose leg a review answer just resolved: the same pairing ingest does, with
    no date limit on which of them are examined (their partner may be months old; each match still needs
    the ±N-day window between the two). Statuses of everything it changed are resolved here.
    Returns absorbed id -> the id of the transaction it merged into."""
    run: _Run = _new_run(session, user_id)
    for txn_id in txn_ids:
        txn: Transaction | None = session.get(Transaction, txn_id)
        if txn is None or txn.id in run.deleted:
            continue
        doubt: str | None = _pair(run, txn)
        if doubt is not None:
            run.doubts[txn.id] = doubt
            run.touched[txn.id] = txn
    for txn in run.touched.values():
        status, txn.review_reason = _status(run, txn)
        if txn.status != "reversed":
            txn.status = status
    session.flush()
    return run.merged_into


# --- statements (tech-design §7) ---------------------------------------------------------------------

def _statement_record(run: _Run, account_id: str, row: StatementRow, identifying: bool = True) -> _Record:
    """One statement row as a record. The account is the statement's own, bound once from its header (§4.7).
    A card bill payment names no payee, so kind_from_legs makes it a transfer, not a refund (ruling 2026-10-19)."""
    payee_raw: str | None = None if row.payment else (row.narration or None)
    return _Record(side="to" if row.direction == "credit" else "from", amount=row.amount, account_id=account_id,
                   unresolved=None, other_account_id=None, payee_raw=payee_raw,
                   payee_id=match_payee(run.rules, payee_raw) if payee_raw else None,
                   reference_number=row.reference if identifying else None, transaction_date=row.date,
                   transaction_time=row.time, sender=None, from_statement=True)


def _identifying(rows: Sequence[StatementRow]) -> list[bool]:
    """Per row, whether its reference can identify it: not when absent, all zeros, or repeated in the statement."""
    refs: list[str | None] = [r.reference for r in rows]
    return [r.reference is not None and r.reference.strip("0") != "" and refs.count(r.reference) == 1 for r in rows]


def match_statement_rows(session: Session, user_id: str, account_id: str,
                         rows: Sequence[StatementRow]) -> list[str | None]:
    """The preview's forecast: per row, the transaction it is another sighting of, or None (new). Each
    transaction is claimed by one row at most. Read-only; the merge works it out again at merge time."""
    run: _Run = _new_run(session, user_id)
    claimed: set[str] = set()
    found: list[str | None] = []
    for row, identifying in zip(rows, _identifying(rows), strict=True):
        txn: Transaction | None = _duplicate_of(run, _statement_record(run, account_id, row, identifying),
                                                exclude=claimed)
        if txn is not None:
            claimed.add(txn.id)
        found.append(txn.id if txn else None)
    return found



@dataclass
class StatementCounts:
    new: int = 0
    matched: int = 0
    needs_review: int = 0


def apply_statement(session: Session, user_id: str, source: Source, account_id: str,
                    rows: Sequence[StatementRow]) -> StatementCounts:
    """§7 step 6, inside the caller's write transaction. Every row is matched again here (a sync may have
    run since the preview), one transaction per row at most. A match takes the statement as its primary
    Source and its amount; the statement's date becomes posted_date while transaction_date keeps the
    email's parsed value (amendment 11), and payee stays as it was (ruling 2026-10-19: statements are
    authoritative on amount and date, PRD §4.2). Edits live in the overlay and are never touched; a
    statement contradicting one is flagged by review_reasons. A row with no match is a new transaction."""
    run: _Run = _new_run(session, user_id)
    identifying: list[bool] = _identifying(rows)
    claimed: set[str] = set()
    matched: int = 0
    for ordinal, row in enumerate(rows):
        record: _Record = _statement_record(run, account_id, row, identifying[ordinal])
        txn: Transaction | None = _duplicate_of(run, record, exclude=claimed)
        if txn is not None:
            matched += 1
            if PRECEDENCE[session.get_one(Source, txn.primary_source_id).kind] <= PRECEDENCE["statement"]:
                txn.primary_source_id = source.id  # over an email, and over an earlier upload of a statement
                txn.amount, txn.posted_date = row.amount, row.date
                txn.reference_number = txn.reference_number or row.reference
                txn.transaction_time = txn.transaction_time or row.time
            _write(txn, record, primary=False)  # fills the statement's own side when it is still empty
            txn.kind = kind_from_legs(session, txn, txn.kind)
        else:
            txn = Transaction(user_id=user_id, primary_source_id=source.id, status="confirmed", posted_date=row.date,
                              derived_key=derived_key(user_id, source.ingest_seq,
                                                      record.reference_number or f"row:{ordinal}"))
            _write(txn, record, primary=True)
            txn.reference_number = row.reference  # kept as printed, even when it identifies nothing
            txn.kind = kind_from_legs(session, txn, "expense" if record.side == "from" else "income")
            session.add(txn)
            session.flush()
            run.created.add(txn.id)
        session.add(TransactionSource(transaction_id=txn.id, source_id=source.id))
        claimed.add(txn.id)
        run.touched[txn.id] = txn
        run.placed.add(txn.id)
    session.flush()
    _settle(run)
    return StatementCounts(new=len(run.created - run.deleted), matched=matched, needs_review=run.counts.needs_review)

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
    """§6.6: needs_review when anything about the transaction is uncertain; why, in words. Read through the
    user's edits (transactions.review), so a re-parse never re-raises a question the user has answered."""
    reasons: list[str] = review_reasons(run.session, txn, run.threshold, run.doubts.get(txn.id))
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
