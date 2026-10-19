"""Why a transaction needs review, worked out from its EFFECTIVE values (tech-design §3.5, §6.6).

One function for the pipeline and for every user action, so the two always
agree: a payee or account the user set by edit is no longer unrecognised or
unresolved, a field with an edit no longer counts as unread, and a
"Looks right" acknowledgement (edit review_acknowledged = the amount) silences
the guardrails until the amount changes: unread fields, a large amount, a
statement disagreeing, an ambiguous transfer match. It never answers which
account or payee it was, or which account paid a card bill. A manual entry is never in review: the
user is its source.

The one reason only the pipeline can produce is an ambiguous transfer match
("possible transfer: …"): it needs the candidate set at pairing time. A user
action keeps it, unless the user has made the transaction a transfer.

Reasons are sentences for the person; tag_of() files each under the review
lane that answers it.
"""

import json
from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from hisaab.accounts.identifiers import normalize_for_lookup
from hisaab.models import ExternalIdentifier, Source, Transaction, TransactionEdit, TransactionEffective
from hisaab.tunables import tunable

CARD_PAYMENT: str = "card payment received: which of your accounts paid it?"
DOUBT: str = "possible transfer"
UNREAD: str = "couldn't read: "
NO_DIRECTION: str = "couldn't tell money in from money out"

SILENCED_BY_LOOKS_RIGHT: frozenset[str] = frozenset({"parse", "large", "statement", "other"})
TAGS: tuple[str, ...] = ("parse", "account", "payee", "large", "transfer", "statement", "other")
_TAG_PREFIXES: tuple[tuple[str, str], ...] = (
    (UNREAD, "parse"), (NO_DIRECTION, "parse"), ("currency ", "parse"),
    ("account ", "account"), ("no account", "account"),
    ("payee not recognised", "payee"), ("no payee", "payee"),
    ("amount over", "large"),
    (DOUBT, "transfer"), (CARD_PAYMENT, "transfer"),
    ("the statement disagrees", "statement"),
)
# A contract field the email left unread, answered by an edit of these fields.
_ANSWERED_BY: dict[str, tuple[str, ...]] = {
    "amount": ("amount",), "date": ("transaction_date",), "payee_raw": ("payee_id",),
    "account_identifier": ("from_account_id", "to_account_id"), "direction": ("kind",),
}


def tag_of(reason: str) -> str:
    return next((tag for prefix, tag in _TAG_PREFIXES if reason.startswith(prefix)), "other")


def _parse_reasons(stored: list[str], edited: set[str]) -> list[str]:
    out: list[str] = []
    for reason in stored:
        if reason.startswith(UNREAD):
            unread: list[str] = [f for f in reason.removeprefix(UNREAD).split(", ")
                                 if not edited & set(_ANSWERED_BY.get(f, ()))]
            if unread:
                out.append(UNREAD + ", ".join(unread))
        elif not (reason == NO_DIRECTION and "kind" in edited):
            out.append(reason)
    return out


def _contradictions(txn: Transaction, statement: Source, edits: Sequence[TransactionEdit]) -> list[str]:
    """§6.6, PRD §4.2: a statement arriving after the user set the amount, and disagreeing. The edit still
    wins; the user decides. An edit made after the statement is the user's answer to it, not a disagreement.
    Amount only: the statement's date is a posting date and never becomes transaction_date (amendment 11)."""
    return ["the statement disagrees with your amount" for e in edits
            if e.field == "amount" and e.new_value != str(txn.amount) and e.updated_at < statement.ingested_at]


def _not_mine(session: Session, user_id: str, raw: str) -> bool:
    canonical: str | None = normalize_for_lookup(raw)
    return canonical is not None and session.get(ExternalIdentifier, (user_id, canonical)) is not None


def review_reasons(session: Session, txn: Transaction, threshold: int, doubt: str | None) -> list[str]:
    """Every reason `txn` needs the user, read through its edits. Empty: confirmed."""
    session.flush()
    t: TransactionEffective = session.scalars(select(TransactionEffective).where(
        TransactionEffective.id == txn.id).execution_options(populate_existing=True)).one()
    primary: Source = session.get_one(Source, t.primary_source_id)
    if primary.kind == "manual":  # the user typed it: nothing to doubt
        return []
    edits: Sequence[TransactionEdit] = session.scalars(select(TransactionEdit).where(
        TransactionEdit.transaction_id == txn.id)).all()
    reasons: list[str] = _parse_reasons(json.loads(primary.diagnostic or "{}").get("review", []),
                                        {e.field for e in edits})
    transfer: bool = t.kind == "transfer"
    if transfer and t.from_account_id is None:
        reasons.append(CARD_PAYMENT)
    unresolved: str | None = t.unresolved_from_identifier or t.unresolved_to_identifier
    if unresolved and _not_mine(session, t.user_id, unresolved):
        pass  # the user said this number isn't one of their accounts: an expense, nothing to ask
    elif unresolved:
        reasons.append(f"account {unresolved} isn't one of yours yet")
    elif t.from_account_id is None and t.to_account_id is None:
        reasons.append("no account named in the email")
    if not transfer and t.payee_id is None:
        reasons.append("payee not recognised" if t.payee_raw else "no payee in the email")
    if t.amount > threshold:
        reasons.append(f"amount over ₹{threshold // 100:,}")
    if primary.kind == "statement":
        reasons.extend(_contradictions(txn, primary, edits))
    if doubt is not None and not transfer:
        reasons.append(doubt)
    if t.review_acknowledged is not None and t.review_acknowledged == str(t.amount):
        # "Looks right" answers the guardrails (§3.5), not a missing account or payee: those need the answer itself
        reasons = [r for r in reasons if tag_of(r) not in SILENCED_BY_LOOKS_RIGHT and not r.startswith(DOUBT)]
    return reasons


def refresh(session: Session, user_id: str, txn_ids: Iterable[str]) -> None:
    """Recompute status after a user action. A reversed transaction keeps its status."""
    threshold: int = tunable(session, "large_amount_threshold_paise", user_id)
    for txn_id in txn_ids:
        txn: Transaction | None = session.get(Transaction, txn_id)
        if txn is None or txn.user_id != user_id:
            continue
        kept: str | None = next((r for r in (txn.review_reason or "").split("; ") if r.startswith(DOUBT)), None)
        reasons: list[str] = review_reasons(session, txn, threshold, kept)
        txn.review_reason = "; ".join(reasons) if reasons else None
        if txn.status != "reversed":
            txn.status = "needs_review" if reasons else "confirmed"
