"""Transactions repository: the only writer of sources, transactions,
transaction_sources and transaction_edits. Every function filters on user_id.

Manual entry (PRD §4.12). What the user types becomes a `manual` Source, its
canonical JSON kept in sources.content, and the transaction derived from it,
exactly as an email alert yields a Source and a transaction. So "the source's
value" means the same thing for every transaction, and later corrections are
overlay edits on all of them alike.

The overlay (tech-design §3.5). One transaction_edits row per (transaction,
field). prior_value is the SOURCE's value, written once when the field is
first edited and never overwritten, so undo always returns to what the source
said, never to an earlier guess. Editing a field back to the source's value, or
undoing it, deletes the row. The transactions row itself is never changed by an
edit. Changes are applied as a set and validated as a whole, so an expense can
become a transfer (kind plus destination) in one step, and an undo that would
leave invalid legs is refused.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, TypeAlias

from sqlalchemy import Row, Select, delete, func, select
from sqlalchemy.orm import Session

from hisaab.accounts import repository as accounts
from hisaab.categories import repository as categories
from hisaab.categories.resolution import categorized
from hisaab.clock import local_today
from hisaab.models import (
    Account,
    Payee,
    Source,
    Transaction,
    TransactionEdit,
    TransactionEffective,
    TransactionSource,
    utc_now,
)
from hisaab.models.core import TRANSACTION_KINDS
from hisaab.payees import repository as payees
from hisaab.transactions import review
from hisaab.transactions.legs import UNRESOLVED, LegError, validate_legs

# Kinds a person types in. Adjustments are written by reconciliation (slice 20).
MANUAL_KINDS: tuple[str, ...] = ("expense", "income", "transfer", "refund")
# Fields the overlay corrects. review_acknowledged is an edit too, but only
# acknowledge() writes it: it is not a value the user types.
EDITABLE_FIELDS: tuple[str, ...] = ("amount", "kind", "transaction_date", "from_account_id", "to_account_id",
                   "payee_id", "category_override_id")
MANUAL_DISCRIMINATOR: str = "0"  # a manual Source yields exactly one transaction

_ISO_DATE: re.Pattern[str] = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME: re.Pattern[str] = re.compile(r"([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?")


class TransactionError(ValueError):
    """A rule violation to show the person (message is user-facing)."""


class EditsWouldBeLost(TransactionError):
    def __init__(self, fields: list[str]):
        super().__init__(f"This entry has corrections ({', '.join(fields)}) that would be lost. "
                         "Confirm to delete it anyway.")
        self.fields = fields


class NotFound(LookupError):
    pass


# An effective transaction with its category_id, category_source, payee_name and provenance.
_ListQuery: TypeAlias = Select[tuple[TransactionEffective, str | None, str | None, str | None, str]]


@dataclass
class ListedTransaction:
    transaction: TransactionEffective  # effective values: the source overlaid with edits
    provenance: str  # kind of the primary Source: email / statement / manual
    edits: list[TransactionEdit]
    category_id: str | None  # resolved at read time (categories.resolution)
    category_source: str | None  # "override" / "rule" / None
    payee_name: str | None


def next_ingest_seq(session: Session) -> int:
    """ingest_seq is global and monotonic; safe because every write holds the writer lock."""
    return (session.scalar(select(func.max(Source.ingest_seq))) or 0) + 1


def derived_key(user_id: str, anchor_ingest_seq: int, discriminator: str) -> str:
    """Stable identity across regeneration (tech-design §3.6): provenance, never content."""
    return hashlib.sha256(f"{user_id}:{anchor_ingest_seq}:{discriminator}".encode()).hexdigest()


# --- validation ----------------------------------------------------------------

def _check_amount(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TransactionError("Amount must be a whole number of paise.")
    if value <= 0:
        raise TransactionError("Amount must be more than zero.")
    if value > accounts.MAX_ABS_PAISE:
        raise TransactionError("That amount is too large.")
    return value


def _check_date(value: object) -> str:
    try:
        if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
            raise ValueError
        parsed: date = date.fromisoformat(value)
    except ValueError:
        raise TransactionError("Date must be a real date (YYYY-MM-DD).") from None
    if parsed > local_today():
        raise TransactionError("The date can't be in the future.")
    return value


def _check_kind(value: object) -> str:
    if value == "lending":
        raise LegError("Lending is not supported yet; record it as an expense.")
    if not isinstance(value, str) or value not in TRANSACTION_KINDS:
        raise TransactionError(f"Unknown kind '{value}'.")
    return value


def _usable_account(session: Session, user_id: str, account_id: str) -> Account:
    """An account the user may put a transaction on now: theirs and not archived."""
    try:
        account: Account = accounts.get_account(session, user_id, account_id)
    except accounts.NotFound:
        raise TransactionError("No such account.") from None
    if account.archived_at is not None:
        raise TransactionError(f"{account.display_name} is archived.")
    return account


def _leg(session: Session, user_id: str, account_id: str | None, unresolved: str | None) -> str | None:
    """A leg as legs.validate_legs sees it: account type, UNRESOLVED, or None (external)."""
    if account_id is not None:
        account: Account | None = session.scalars(
            select(Account).where(Account.id == account_id, Account.user_id == user_id)
        ).one_or_none()
        if account is None:
            raise TransactionError("No such account.")
        return account.type
    return UNRESOLVED if unresolved else None


# --- reads ----------------------------------------------------------------------

def _own_transaction(session: Session, user_id: str, txn_id: str) -> Transaction:
    txn: Transaction | None = session.scalars(
        select(Transaction).where(Transaction.id == txn_id, Transaction.user_id == user_id)
    ).one_or_none()
    if txn is None:
        raise NotFound("No such transaction")
    return txn


def get_effective(session: Session, user_id: str, txn_id: str) -> TransactionEffective:
    row: TransactionEffective | None = session.scalars(
        select(TransactionEffective).where(TransactionEffective.id == txn_id, TransactionEffective.user_id == user_id)
    ).one_or_none()
    if row is None:
        raise NotFound("No such transaction")
    return row


def _edits(session: Session, user_id: str, txn_ids: list[str]) -> dict[str, list[TransactionEdit]]:
    grouped: dict[str, list[TransactionEdit]] = {t: [] for t in txn_ids}
    for e in session.scalars(
        select(TransactionEdit)
        .where(TransactionEdit.transaction_id.in_(txn_ids), TransactionEdit.user_id == user_id)
        .order_by(TransactionEdit.field)
    ):
        grouped[e.transaction_id].append(e)
    return grouped


def _listed(session: Session, user_id: str, query: _ListQuery) -> list[ListedTransaction]:
    rows: Sequence[Row[Any]] = session.execute(query).all()
    edits: dict[str, list[TransactionEdit]] = _edits(session, user_id, [r.TransactionEffective.id for r in rows])
    return [ListedTransaction(r.TransactionEffective, r.provenance, edits[r.TransactionEffective.id],
                              r.category_id, r.category_source, r.payee_name) for r in rows]


def with_provenance(user_id: str) -> _ListQuery:
    """The categorized select plus provenance (the primary Source's kind): the list and the report read through it."""
    return (categorized(user_id).add_columns(Source.kind.label("provenance"))
            .join(Source, Source.id == TransactionEffective.primary_source_id))


def list_transactions(session: Session, user_id: str, limit: int = 200, start: str | None = None,
                      end: str | None = None) -> list[ListedTransaction]:
    """The user's transactions, newest first, with provenance, category and corrections attached; optionally
    only those dated start..end (both included, either end open)."""
    query: _ListQuery = with_provenance(user_id)
    if start is not None:
        query = query.where(TransactionEffective.transaction_date >= start)
    if end is not None:
        query = query.where(TransactionEffective.transaction_date <= end)
    return _listed(session, user_id, query.order_by(
        TransactionEffective.transaction_date.desc(), TransactionEffective.created_at.desc()).limit(limit))


def list_by_ids(session: Session, user_id: str, txn_ids: list[str]) -> dict[str, ListedTransaction]:
    """These of the user's transactions that still exist, by id (a statement preview's matches)."""
    found: list[ListedTransaction] = _listed(
        session, user_id, with_provenance(user_id).where(TransactionEffective.id.in_(txn_ids)))
    return {t.transaction.id: t for t in found}


def get_listed(session: Session, user_id: str, txn_id: str) -> ListedTransaction:
    found: list[ListedTransaction] = _listed(
        session, user_id, with_provenance(user_id).where(TransactionEffective.id == txn_id))
    if not found:
        raise NotFound("No such transaction")
    return found[0]


# --- manual entry --------------------------------------------------------------

def create_manual(
    session: Session,
    user_id: str,
    *,
    kind: str,
    amount: int,
    transaction_date: str,
    from_account_id: str | None = None,
    to_account_id: str | None = None,
    payee_raw: str | None = None,
    transaction_time: str | None = None,
    category_id: str | None = None,
) -> Transaction:
    """Record a manual entry. A typed payee name links to the payee of that name
    (created if new). A category pick follows the ruling of 2026-10-11: the
    payee's first pick becomes its rule (Loop 2); a pick that differs from an
    existing rule overrides just this entry; with no payee it's an override."""
    if kind not in MANUAL_KINDS:
        raise TransactionError("A manual entry is an expense, income, a transfer or a refund.")
    amount = _check_amount(amount)
    transaction_date = _check_date(transaction_date)
    if transaction_time is not None and not _TIME.fullmatch(transaction_time):
        raise TransactionError("Time must be HH:MM.")
    from_type: str | None = _usable_account(session, user_id, from_account_id).type if from_account_id else None
    to_type: str | None = _usable_account(session, user_id, to_account_id).type if to_account_id else None
    validate_legs(kind, from_type, to_type, same_account=from_account_id is not None and from_account_id == to_account_id)
    payee_raw = (payee_raw or "").strip() or None
    if category_id is not None:
        _canonical(session, user_id, "category_override_id", category_id)  # theirs and active, before any write
    try:
        payee: Payee | None = payees.get_or_create_by_name(session, payee_raw) if payee_raw else None
    except payees.PayeeError as exc:
        raise TransactionError(str(exc)) from None

    entered: dict[str, str | int | None] = {"kind": kind, "amount": amount, "transaction_date": transaction_date,
               "transaction_time": transaction_time, "from_account_id": from_account_id,
               "to_account_id": to_account_id, "payee_raw": payee_raw, "category_id": category_id}
    content: bytes = json.dumps(entered, sort_keys=True, separators=(",", ":")).encode()
    now: str = utc_now()
    seq: int = next_ingest_seq(session)
    source: Source = Source(ingest_seq=seq, user_id=user_id, kind="manual", content=content,
                    content_hash=hashlib.sha256(content).hexdigest(), received_at=now, ingested_at=now,
                    processing_state="parsed")
    session.add(source)
    session.flush()
    txn: Transaction = Transaction(
        user_id=user_id, derived_key=derived_key(user_id, seq, MANUAL_DISCRIMINATOR), amount=amount, kind=kind,
        from_account_id=from_account_id, to_account_id=to_account_id, payee_raw=payee_raw,
        payee_id=payee.id if payee else None,
        transaction_date=transaction_date, transaction_time=transaction_time,
        status="confirmed",  # typed by the user, not parsed: nothing to review (ruling 2026-10-11)
        primary_source_id=source.id,
    )
    session.add(txn)
    session.flush()
    session.add(TransactionSource(transaction_id=txn.id, source_id=source.id))
    session.flush()
    if category_id is not None:
        rule: str | None = categories.payee_rule(session, user_id, payee.id) if payee else None
        if payee and rule is None:
            categories.set_payee_category(session, user_id, payee.id, category_id)  # learn
        elif rule != category_id:
            session.add(TransactionEdit(transaction_id=txn.id, user_id=user_id, field="category_override_id",
                                        prior_value=None, new_value=category_id))  # just this entry
            session.flush()
    return txn


# --- the overlay ---------------------------------------------------------------

def _source_value(txn: Transaction, field: str) -> str | None:
    if field == "category_override_id":
        return None  # no source ever says a category; it exists only as an edit (tech-design §3.5)
    value: object = getattr(txn, field)
    return None if value is None else str(value)


def _canonical(session: Session, user_id: str, field: str, value: object) -> str | None:
    """The value as stored in transaction_edits.new_value, validated."""
    if field not in EDITABLE_FIELDS:
        raise TransactionError(f"'{field}' can't be edited.")
    if field == "amount":
        return str(_check_amount(value))
    if field == "kind":
        return _check_kind(value)
    if field == "transaction_date":
        return _check_date(value)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TransactionError(f"'{field}' must be an id.")
    if field == "payee_id":
        try:
            return payees.get_payee(session, value).id  # payees are global
        except payees.NotFound:
            raise TransactionError("No such payee.") from None
    if field == "category_override_id":
        try:
            return categories.active_category(session, user_id, value).id
        except categories.NotFound:
            raise TransactionError("No such category.") from None
        except categories.CategoryError as exc:
            raise TransactionError(str(exc)) from None
    return _usable_account(session, user_id, value).id  # from/to account


def _check_effective(session: Session, user_id: str, txn: Transaction, values: dict[str, str | None]) -> None:
    """Validate the transaction as it would read with `values` (field -> canonical) overlaid."""
    def get(field: str) -> str | None:
        return values[field] if field in values else _source_value(txn, field)

    from_id: str | None = get("from_account_id")
    to_id: str | None = get("to_account_id")
    # Assigning a leg's account clears its unresolved identifier (as the view does).
    from_leg: str | None = _leg(
        session, user_id, from_id, None if "from_account_id" in values else txn.unresolved_from_identifier)
    to_leg: str | None = _leg(
        session, user_id, to_id, None if "to_account_id" in values else txn.unresolved_to_identifier)
    validate_legs(get("kind") or "", from_leg, to_leg, same_account=from_id is not None and from_id == to_id)


def apply_edits(session: Session, user_id: str, txn_id: str, changes: dict[str, object]) -> None:
    """Set several fields' effective values at once; validated together, written together."""
    txn: Transaction = _own_transaction(session, user_id, txn_id)
    existing: dict[str, TransactionEdit] = {e.field: e for e in _edits(session, user_id, [txn_id])[txn_id]}
    wanted: dict[str, str | None] = {
        field: _canonical(session, user_id, field, value) for field, value in changes.items()}
    _check_effective(session, user_id, txn, {f: e.new_value for f, e in existing.items()} | wanted)

    for field, new_value in wanted.items():
        source_value: str | None = _source_value(txn, field)
        edit: TransactionEdit | None = existing.get(field)
        if new_value == source_value:  # back to what the source said: no correction left
            if edit is not None:
                session.delete(edit)
        elif edit is not None:
            edit.new_value = new_value  # prior_value stays the source's value
        else:
            session.add(TransactionEdit(transaction_id=txn.id, user_id=user_id, field=field,
                                        prior_value=source_value, new_value=new_value))
    session.flush()
    review.refresh(session, user_id, [txn.id])  # a correction may answer (or raise) a review question


def acknowledge(session: Session, user_id: str, txn_id: str) -> None:
    """"Looks right": silences this transaction's review reasons until its amount changes (tech-design §3.5)."""
    txn: Transaction = _own_transaction(session, user_id, txn_id)
    amount: str = str(get_effective(session, user_id, txn_id).amount)
    edit: TransactionEdit | None = session.scalars(select(TransactionEdit).where(
        TransactionEdit.transaction_id == txn.id, TransactionEdit.field == "review_acknowledged")).first()
    if edit is None:
        session.add(TransactionEdit(transaction_id=txn.id, user_id=user_id, field="review_acknowledged",
                                    prior_value=None, new_value=amount))
    else:
        edit.new_value = amount
    session.flush()
    review.refresh(session, user_id, [txn.id])


def revert(session: Session, user_id: str, txn_id: str, fields: list[str]) -> None:
    """Undo corrections: those fields read the source's value again."""
    txn: Transaction = _own_transaction(session, user_id, txn_id)
    for field in fields:
        if field not in EDITABLE_FIELDS:
            raise TransactionError(f"'{field}' can't be edited.")
    existing: dict[str, TransactionEdit] = {e.field: e for e in _edits(session, user_id, [txn_id])[txn_id]}
    remaining: dict[str, str | None] = {f: e.new_value for f, e in existing.items() if f not in fields}
    _check_effective(session, user_id, txn, remaining)
    for field in fields:
        if field in existing:
            session.delete(existing[field])
    session.flush()
    review.refresh(session, user_id, [txn.id])


# --- delete --------------------------------------------------------------------

def delete_manual(session: Session, user_id: str, txn_id: str, *, confirm_edits_lost: bool = False) -> None:
    """Delete a manual entry with its Source (PRD §10.6: transactions go with their Source).

    Corrections on it would be lost, so they need explicit confirmation; the
    transaction_edits FK deliberately has no cascade (ruling 2026-10-10).
    """
    txn: Transaction = _own_transaction(session, user_id, txn_id)
    source: Source | None = session.get(Source, txn.primary_source_id)
    assert source is not None  # primary_source_id is a NOT NULL foreign key
    if source.kind != "manual":
        raise TransactionError("Only manual entries can be deleted. Bank alerts and statements stay as received.")
    fields: list[str] = [e.field for e in _edits(session, user_id, [txn_id])[txn_id]]
    if fields and not confirm_edits_lost:
        raise EditsWouldBeLost(fields)
    # Explicit order: no ORM relationships tell SQLAlchemy which rows depend on which.
    session.execute(delete(TransactionEdit).where(TransactionEdit.transaction_id == txn.id))
    session.execute(delete(TransactionSource).where(TransactionSource.transaction_id == txn.id))
    session.execute(delete(Transaction).where(Transaction.id == txn.id))
    session.execute(delete(Source).where(Source.id == source.id, Source.user_id == user_id))
