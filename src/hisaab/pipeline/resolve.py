"""Answers from review applied to rows already stored: the pipeline's own work, done in place.

- An identifier assigned to an account (PRD §3.3): every transaction whose leg
  carries it gets that account, kind re-derived from the legs, and transfer
  pairing re-run on them (a card's credit can now meet its bank debit). Future emails
  resolve at parse through the registry row the caller just wrote.
- A normalization pattern (tech-design §6.4: "applying a new pattern
  retroactively is a pure database operation"): every unnamed transaction
  whose raw payee string it matches gets its payee. Patterns are global, so
  this reaches every user's rows.

- A name for a payee's text (name_text): the admin's global pattern, or a
  member's edits.

All refresh status, since the answer may take the row out of review. Neither
touches the overlay: an edit on the same field still wins in the effective view.
"""

from collections import defaultdict

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from hisaab.accounts.identifiers import normalize_for_lookup
from hisaab.accounts.repository import AccountError
from hisaab.models import Account, NormalizationPattern, Source, Transaction, TransactionEffective
from hisaab.payees.normalization import PatternRule, match_payee
from hisaab.pipeline.derive import LEGS, kind_from_legs, pair_resolved
from hisaab.transactions.repository import apply_edits
from hisaab.transactions.review import refresh


def resolve_identifier_everywhere(session: Session, user_id: str, account: Account, canonical: str) -> list[str]:
    """Give `account` to every leg of the user's transactions that carries `canonical`; the ids touched."""
    rows: list[Transaction] = list(session.scalars(select(Transaction).where(
        Transaction.user_id == user_id, or_(Transaction.unresolved_from_identifier.is_not(None),
                                            Transaction.unresolved_to_identifier.is_not(None)))))
    touched: list[str] = []
    for txn in rows:
        for leg, unresolved in LEGS.values():
            raw: str | None = getattr(txn, unresolved)
            if raw is not None and normalize_for_lookup(raw) == canonical and getattr(txn, leg) is None:
                setattr(txn, leg, account.id)
                setattr(txn, unresolved, None)
                if txn.id not in touched:
                    touched.append(txn.id)
        if txn.id in touched:
            txn.kind = kind_from_legs(session, txn, txn.kind)
    session.flush()
    merged: dict[str, str] = pair_resolved(session, user_id, touched)  # a resolved leg may complete a transfer
    touched = list(dict.fromkeys(merged.get(t, t) for t in touched))
    refresh(session, user_id, touched)
    return touched


def retyped(session: Session, user_id: str, account: Account) -> list[str]:
    """`account` just switched bank <-> card: what its alerts meant changes with it (a credit into a card is a
    refund or a bill paid, never income). Re-derive the kind of every emailed or statement transaction on it,
    pair again, and refuse (rolling everything back) when something the user typed can't fit: income on a card."""
    rows: list[Transaction] = list(session.scalars(
        select(Transaction).join(Source, Source.id == Transaction.primary_source_id).where(
            Transaction.user_id == user_id, Source.kind != "manual",
            or_(Transaction.from_account_id == account.id, Transaction.to_account_id == account.id))))
    touched: list[str] = []
    for txn in rows:
        kind: str = kind_from_legs(session, txn, txn.kind)
        if kind != txn.kind:
            txn.kind = kind
            touched.append(txn.id)
    session.flush()
    merged: dict[str, str] = pair_resolved(session, user_id, touched)
    touched = list(dict.fromkeys(merged.get(t, t) for t in touched))
    refresh(session, user_id, touched)
    if account.type == "credit_card":
        income: int = len(session.scalars(select(TransactionEffective.id).where(
            TransactionEffective.user_id == user_id, TransactionEffective.to_account_id == account.id,
            TransactionEffective.kind == "income", TransactionEffective.status != "reversed")).all())
        if income:
            raise AccountError(f"{income} {'entry records' if income == 1 else 'entries record'} income into "
                               f"{account.display_name}, and a card can't receive income. Change "
                               f"{'it' if income == 1 else 'them'} to a refund first.")
    return touched


def renormalize(session: Session, rule: PatternRule) -> dict[str, list[str]]:
    """Apply one pattern to every unnamed transaction it matches; user id -> the ids touched."""
    touched: defaultdict[str, list[str]] = defaultdict(list)
    for txn in session.scalars(select(Transaction).where(Transaction.payee_id.is_(None),
                                                         Transaction.payee_raw.is_not(None))):
        if match_payee([rule], txn.payee_raw or "") is not None:
            txn.payee_id = rule.payee_id
            touched[txn.user_id].append(txn.id)
    session.flush()
    for user_id, ids in touched.items():
        refresh(session, user_id, ids)
    return dict(touched)


PATTERN_PRIORITY: int = 100  # a name given in review beats the shipped contains-patterns (all priority 0)


def name_text(session: Session, user_id: str, is_admin: bool, raw: str, payee_id: str) -> tuple[list[str], bool]:
    """Name every transaction carrying this payee text (ruling 2026-10-12): an admin writes one exact global
    pattern, so other users' rows and future ones resolve too; a member's naming is edits on their own rows,
    kept as corrections for the admin. Returns the caller's transaction ids named, and whether a pattern was
    written."""
    rule: PatternRule = PatternRule("review", "exact", raw, payee_id, PATTERN_PRIORITY)
    if is_admin:
        session.add(NormalizationPattern(match_type="exact", pattern=raw, payee_id=payee_id, origin="admin",
                                         priority=PATTERN_PRIORITY))
        session.flush()
        return renormalize(session, rule).get(user_id, []), True
    mine: list[str] = [t.id for t in session.scalars(select(TransactionEffective).where(
        TransactionEffective.user_id == user_id, TransactionEffective.payee_id.is_(None),
        TransactionEffective.payee_raw.is_not(None))) if match_payee([rule], t.payee_raw or "")]
    for txn_id in mine:
        apply_edits(session, user_id, txn_id, {"payee_id": payee_id})
    return mine, False
