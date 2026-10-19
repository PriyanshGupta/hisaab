"""/api/review: the review queue and the answers that settle many transactions at once (PRD §5.5).

The queue is every transaction that is in review (status needs_review, with
reasons) or uncategorized (an expense, income or refund with no category).
Each item carries tags naming the lane that answers it: account, payee,
uncategorized, and the per-item checks (parse, large, transfer, statement).

Grouped answers write a rule or a registry row, never an edit per transaction:
- payee-category: ONE payee_categories row; overrides are kept and counted
  ("23 transactions updated, 1 override kept").
- identifier: ONE account_identifiers row; every past transaction carrying it
  resolves now, future ones at parse (PRD §3.3).
- name-payee: an admin writes one exact global pattern and every matching
  transaction resolves; a member's naming is edits on their own matching
  rows, kept as corrections for the admin (ruling 2026-10-12, PRD §5.1).
Per-item answers are the transaction endpoints (PATCH, acknowledge).
"""

import json
import logging
import re
from email import policy
from email.parser import BytesParser
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import Subquery, and_, delete, func, or_, select
from sqlalchemy.orm import Session

from hisaab.accounts import repository as accounts
from hisaab.accounts.identifiers import normalize_for_lookup
from hisaab.api.deps import CurrentUser, current_user, get_db
from hisaab.categories import auto
from hisaab.categories import repository as cats
from hisaab.categories.resolution import categorized
from hisaab.db import Database
from hisaab.llm import ContractError, LLMUnavailable
from hisaab.models import (
    Account,
    AccountIdentifier,
    ExternalIdentifier,
    GmailConnection,
    NormalizationPattern,
    Payee,
    PayeeCategory,
    PayeeSuggestion,
    RawPayeeSuggestion,
    Source,
    TransactionEdit,
    TransactionEffective,
)
from hisaab.payees import repository as payees
from hisaab.parsing.email_interpreter import parse_eml
from hisaab.parsing.transforms import strip_html
from hisaab.payees.normalization import PatternRule, match_payee
from hisaab.pipeline.resolve import PATTERN_PRIORITY, name_text, resolve_identifier_everywhere
from hisaab.transactions import repository as txns
from hisaab.transactions.review import refresh, tag_of

router: APIRouter = APIRouter(prefix="/api/review", tags=["review"])
_log: logging.Logger = logging.getLogger(__name__)

CATEGORIZABLE: tuple[str, ...] = ("expense", "income", "refund")  # transfers and adjustments never take one


class ReviewItem(BaseModel):
    id: str
    transaction_date: str
    amount_paise: int
    kind: str
    payee_raw: str | None
    payee_id: str | None
    payee_name: str | None
    category_id: str | None
    account_label: str | None  # the account's name, or "ending 4821" for an identifier not yet assigned
    identifier: str | None  # the unassigned identifier, canonical: what an Accounts-lane answer is for
    account_leg: Literal["from", "to"]  # the side an account answer fills: money left it, or reached it
    tags: list[str]
    reason: str | None
    status: str
    email: EmailOut | None  # where it came from; None for manual entries and statements


class EmailOut(BaseModel):
    received_at: str  # UTC
    sender: str | None
    subject: str | None  # None when the email's text wasn't kept
    has_text: bool  # GET /api/review/email/{id} can show it
    gmail_url: str | None  # opens the message in Gmail, signed in as the connected address


class SortedAccount(BaseModel):
    key: str
    label: str  # the numbers that find it: "Ending 1111, ending 4821"
    account_id: str | None  # None: numbers the user set aside ("not my account")
    answer: str | None  # the account's name
    items: list[ReviewItem]


class SortedPayee(BaseModel):
    key: str
    label: str  # the text named, or "Unknown payee"
    payee_id: str
    payee_name: str
    items: list[ReviewItem]


class CategorizedPayee(BaseModel):
    """A payee with a rule: shown below what's left to sort, still changeable (user, 2026-10-19)."""
    payee_id: str
    payee_name: str
    category_id: str
    origin: str | None  # user | llm (auto-categorized) | None (before slice 17)
    items: list[ReviewItem]  # its expenses, income and refunds


class ReviewOut(BaseModel):
    items: list[ReviewItem]  # what's left to sort
    categorized: list[CategorizedPayee]  # payees already filed under a category
    sorted_accounts: list[SortedAccount]  # accounts alerts now land on, and numbers set aside
    sorted_payees: list[SortedPayee]  # texts named in review


class PayeeCategoryIn(BaseModel):
    payee_id: str
    category_id: str


class PayeeCategoryOut(BaseModel):
    updated: int  # now follow the rule
    overrides_kept: int  # carry their own category and keep it


class IdentifierIn(BaseModel):
    identifier: str
    account_id: str


class IdentifierOut(BaseModel):
    resolved: int


class NamePayeeIn(BaseModel):
    payee_raw: str | None = None  # the text to name: every transaction carrying it, or
    transaction_ids: list[str] | None = None  # these, when the email named no payee at all
    payee_id: str | None = None  # an existing payee, or
    name: str | None = None  # a name: found, or created


class AssignAccountIn(BaseModel):
    transaction_ids: list[str]  # "Unknown account": the email named none, so these only, by edit
    account_id: str


class UpdatedOut(BaseModel):
    updated: int


class NamePayeeOut(BaseModel):
    updated: int  # the caller's transactions now carrying the payee
    payee_id: str
    pattern: bool  # a global pattern was written (admin): future transactions resolve too


def _label(identifier: str) -> str:
    return f"ending {identifier}" if identifier.isdigit() else identifier


def own_leg(t: Any) -> Literal["from", "to"]:
    """The side an account answer fills: money left it (an expense, a card bill's payer) or reached it."""
    return "from" if t.unresolved_from_identifier or t.kind == "expense" or (
        t.kind == "transfer" and t.from_account_id is None) else "to"


def _account_label(t: Any, names: dict[str, str], identifier: str | None) -> str | None:
    if t.kind == "transfer" and t.from_account_id and t.to_account_id:
        return f"{names.get(t.from_account_id, '?')} to {names.get(t.to_account_id, '?')}"
    own: str | None = t.from_account_id or t.to_account_id
    if own is not None:
        return names.get(own)
    return _label(identifier) if identifier else None


@router.get("", response_model=ReviewOut)
def queue(user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)) -> ReviewOut:
    q: Subquery = categorized(user.id).subquery()
    with db.read_session() as s:
        names: dict[str, str] = dict(s.execute(select(Account.id, Account.display_name).where(
            Account.user_id == user.id)).tuples().all())
        rows: list[Any] = list(s.execute(select(q).order_by(q.c.transaction_date.desc(), q.c.id)))
        emails: dict[str, EmailOut] = email_details(s, user.id, [t.primary_source_id for t in rows])
        rules: list[tuple[str, str, str | None]] = list(s.execute(select(
            PayeeCategory.payee_id, PayeeCategory.category_id, PayeeCategory.origin).where(
            PayeeCategory.user_id == user.id)).tuples())
        identifiers: list[tuple[str, str]] = list(s.execute(
            select(AccountIdentifier.account_id, AccountIdentifier.identifier)
            .join(Account, Account.id == AccountIdentifier.account_id)
            .where(AccountIdentifier.user_id == user.id, AccountIdentifier.retired_at.is_(None),
                   Account.archived_at.is_(None)).order_by(AccountIdentifier.created_at)).tuples())
        not_mine: list[str] = list(s.scalars(select(ExternalIdentifier.identifier).where(
            ExternalIdentifier.user_id == user.id).order_by(ExternalIdentifier.created_at)))
        named_raw: list[tuple[str, str]] = list(s.execute(select(NormalizationPattern.pattern, NormalizationPattern.payee_id)
            .where(NormalizationPattern.origin == "admin", NormalizationPattern.match_type == "exact")).tuples())
        edited_payee: set[str] = set(s.scalars(select(TransactionEdit.transaction_id).where(
            TransactionEdit.user_id == user.id, TransactionEdit.field == "payee_id")))
    every: list[tuple[Any, ReviewItem]] = [(t, _item(t, names, emails.get(t.primary_source_id))) for t in rows]
    items: list[ReviewItem] = [i for t, i in every if t.status == "needs_review" or (
        t.category_id is None and t.kind in CATEGORIZABLE)]
    return ReviewOut(items=items, categorized=_categorized(every, rules),
                     sorted_accounts=_sorted_accounts(every, identifiers, not_mine, names),
                     sorted_payees=_sorted_payees(every, named_raw, edited_payee))


def _categorized(every: list[tuple[Any, ReviewItem]], rules: list[tuple[str, str, str | None]]) -> list[CategorizedPayee]:
    by_payee: dict[str, list[ReviewItem]] = {}
    names_of: dict[str, str] = {}
    for t, i in every:
        if t.payee_id is not None and t.kind in CATEGORIZABLE:
            by_payee.setdefault(t.payee_id, []).append(i)
            names_of[t.payee_id] = t.payee_name or ""
    return sorted((CategorizedPayee(payee_id=p, payee_name=names_of[p], category_id=c, origin=o, items=by_payee[p])
                   for p, c, o in rules if p in by_payee), key=lambda c: (-len(c.items), c.payee_name))


def _sorted_accounts(every: list[tuple[Any, ReviewItem]], identifiers: list[tuple[str, str]], not_mine: list[str],
                     names: dict[str, str]) -> list[SortedAccount]:
    """Each account the user's alerts now land on, with the numbers that find it; then numbers set aside."""
    labels: dict[str, list[str]] = {}
    for account_id, identifier in identifiers:
        labels.setdefault(account_id, []).append(_label(identifier))
    out: list[SortedAccount] = []
    for account_id, ids in labels.items():
        mine: list[ReviewItem] = [i for t, i in every if account_id in (t.from_account_id, t.to_account_id)]
        if mine:
            out.append(SortedAccount(key=account_id, label=_capital(", ".join(ids)), account_id=account_id,
                                     answer=names.get(account_id), items=mine))
    for identifier in not_mine:
        kept: list[ReviewItem] = [i for t, i in every if i.identifier == identifier]
        if kept:
            out.append(SortedAccount(key=f"not-mine:{identifier}", label=_capital(_label(identifier)), account_id=None,
                                     answer=None, items=kept))
    return out


def _sorted_payees(every: list[tuple[Any, ReviewItem]], named_raw: list[tuple[str, str]],
                   edited_payee: set[str]) -> list[SortedPayee]:
    """Raw texts named in review (a pattern), and transactions given a payee by edit."""
    groups: dict[tuple[str, str], SortedPayee] = {}
    rules: list[PatternRule] = [PatternRule(raw, "exact", raw, payee, PATTERN_PRIORITY) for raw, payee in named_raw]
    for t, i in every:
        if t.payee_id is None:
            continue
        rule: PatternRule | None = next((r for r in rules if r.payee_id == t.payee_id and t.payee_raw
                                         and match_payee([r], t.payee_raw)), None)
        if rule is None and t.id not in edited_payee:
            continue
        label: str = rule.pattern if rule else (t.payee_raw or "Unknown payee")
        group: SortedPayee = groups.setdefault((label, t.payee_id), SortedPayee(
            key=f"{label}|{t.payee_id}", label=label, payee_id=t.payee_id, payee_name=t.payee_name or "", items=[]))
        group.items.append(i)
    return sorted(groups.values(), key=lambda g: (-len(g.items), g.label))


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def email_details(session: Session, user_id: str, source_ids: list[str]) -> dict[str, EmailOut]:
    """Where each transaction came from, for the person checking it: when, from whom, and a way to open it."""
    connection: GmailConnection | None = session.get(GmailConnection, user_id)
    account: str = f"?authuser={quote(connection.email_address, safe='')}" if connection else ""
    out: dict[str, EmailOut] = {}
    for src in session.scalars(select(Source).where(Source.user_id == user_id, Source.id.in_(set(source_ids)),
                                                    Source.kind == "email")):
        subject: str | None = None
        if src.content is not None:  # headers only: cheap, and the subject is what a person recognises
            subject = str(BytesParser(policy=policy.default).parsebytes(src.content, headersonly=True)["Subject"] or "") or None
        out[src.id] = EmailOut(
            received_at=src.received_at, sender=json.loads(src.diagnostic or "{}").get("sender"), subject=subject,
            has_text=src.content is not None,
            gmail_url=f"https://mail.google.com/mail/{account}#all/{quote(src.external_message_id, safe='')}"
            if src.external_message_id else None)
    return out


def _item(t: Any, names: dict[str, str], email: "EmailOut | None") -> ReviewItem:
    raw: str | None = t.unresolved_from_identifier or t.unresolved_to_identifier
    identifier: str | None = normalize_for_lookup(raw) if raw else None
    tags: list[str] = list(dict.fromkeys(tag_of(r) for r in (t.review_reason or "").split("; ") if r))
    if t.category_id is None and t.kind in CATEGORIZABLE:
        tags.append("uncategorized")
    return ReviewItem(
        id=t.id, transaction_date=t.transaction_date, amount_paise=t.amount, kind=t.kind, payee_raw=t.payee_raw,
        payee_id=t.payee_id, payee_name=t.payee_name, category_id=t.category_id,
        account_label=_account_label(t, names, identifier), identifier=identifier,
        account_leg=own_leg(t), tags=tags,
        reason=t.review_reason, status=t.status, email=email)


class EmailText(BaseModel):
    text: str


@router.get("/email/{txn_id}", response_model=EmailText)
def email_text(txn_id: str, user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)) -> EmailText:
    """The email a transaction came from, as text, when Hisaab kept it (emails read by a template are not kept)."""
    with db.read_session() as s:
        t: TransactionEffective = txns.get_effective(s, user.id, txn_id)
        src: Source = s.get_one(Source, t.primary_source_id)
        if src.content is None or src.kind != "email":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Hisaab didn't keep this email; open it in Gmail.")
        body: str = parse_eml(src.content).body
    text: str = re.sub(r"\n\s*\n+", "\n\n", strip_html(body)).strip()
    return EmailText(text=text)


@router.post("/payee-category", response_model=PayeeCategoryOut)
def categorize_payee(body: PayeeCategoryIn, request: Request, user: CurrentUser = Depends(current_user),
                     db: Database = Depends(get_db)) -> PayeeCategoryOut:
    """"Categorize all of these": one rule for the payee, past and future (PRD §3.11)."""
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        cats.set_payee_category(s, user.id, body.payee_id, body.category_id)
        followed, kept = s.execute(select(
            func.count().filter(TransactionEffective.category_override_id.is_(None)),
            func.count().filter(TransactionEffective.category_override_id.is_not(None)),
        ).where(TransactionEffective.user_id == user.id, TransactionEffective.payee_id == body.payee_id,
                TransactionEffective.kind.in_(CATEGORIZABLE))).one()
    return PayeeCategoryOut(updated=followed, overrides_kept=kept)


@router.post("/identifier", response_model=IdentifierOut)
def assign_identifier(body: IdentifierIn, request: Request, user: CurrentUser = Depends(current_user),
                      db: Database = Depends(get_db)) -> IdentifierOut:
    """"Ending 4821 is my HDFC savings": every past and future transaction carrying it resolves."""
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        _, canonical = accounts.assign_identifier(s, user.id, body.account_id, body.identifier)
        account: Account = accounts.get_account(s, user.id, body.account_id)
        return IdentifierOut(resolved=len(resolve_identifier_everywhere(s, user.id, account, canonical)))


@router.post("/assign-account", response_model=UpdatedOut)
def assign_account(body: AssignAccountIn, request: Request, user: CurrentUser = Depends(current_user),
                   db: Database = Depends(get_db)) -> UpdatedOut:
    """The email named no account: these transactions get one by edit (there is nothing to register)."""
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        legs: dict[str, str] = {i: own_leg(txns.get_effective(s, user.id, i)) for i in body.transaction_ids}
        for txn_id, leg in legs.items():
            txns.apply_edits(s, user.id, txn_id, {f"{leg}_account_id": body.account_id})
        return UpdatedOut(updated=len(legs))


class NotMineIn(BaseModel):
    identifier: str


class NotMineOut(BaseModel):
    quieted: int  # transactions no longer asking which account it is


@router.post("/not-my-account", response_model=NotMineOut)
def not_my_account(body: NotMineIn, request: Request, user: CurrentUser = Depends(current_user),
                   db: Database = Depends(get_db)) -> NotMineOut:
    """"Not my account — keep as expenses" (a FASTag wallet): remembered per user, past and future."""
    canonical: str | None = normalize_for_lookup(body.identifier)
    if canonical is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "There is no number to set aside.")
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        if s.get(ExternalIdentifier, (user.id, canonical)) is None:
            s.add(ExternalIdentifier(user_id=user.id, identifier=canonical))
            s.flush()
        rows: list[str] = [t.id for t in s.scalars(select(TransactionEffective).where(
            TransactionEffective.user_id == user.id, or_(TransactionEffective.unresolved_from_identifier.is_not(None),
                                                         TransactionEffective.unresolved_to_identifier.is_not(None))))
            if normalize_for_lookup(t.unresolved_from_identifier or t.unresolved_to_identifier or "") == canonical]
        refresh(s, user.id, rows)
        return NotMineOut(quieted=len(rows))


def _payee(session: Session, body: NamePayeeIn) -> Payee:
    if (body.payee_id is None) == (body.name is None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Give either an existing payee or a name.")
    return payees.get_payee(session, body.payee_id) if body.payee_id else payees.get_or_create_by_name(
        session, body.name or "")


@router.post("/name-payee", response_model=NamePayeeOut)
def name_payee(body: NamePayeeIn, request: Request, user: CurrentUser = Depends(current_user),
               db: Database = Depends(get_db)) -> NamePayeeOut:
    if body.transaction_ids is not None:  # no text to match on: edits on exactly these, never a pattern
        with db.write_session(timeout=request.app.state.writer_timeout) as s:
            for txn_id in body.transaction_ids:
                txns.get_effective(s, user.id, txn_id)  # 404 for anything not the caller's, before any write
            named: Payee = _payee(s, body)
            for txn_id in body.transaction_ids:
                txns.apply_edits(s, user.id, txn_id, {"payee_id": named.id})
            return NamePayeeOut(updated=len(body.transaction_ids), payee_id=named.id, pattern=False)
    if not (body.payee_raw or "").strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "There is no payee text to name.")
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        payee: Payee = _payee(s, body)
        named_ids, pattern = name_text(s, user.id, user.role == "admin", body.payee_raw or "", payee.id)
        return NamePayeeOut(updated=len(named_ids), payee_id=payee.id, pattern=pattern)


# --- auto-categorize (the user's slice 17 brief) -------------------------------------------------

class AppliedOut(BaseModel):
    payee_id: str
    payee_name: str
    category_id: str
    confidence_percent: int
    transactions: int  # filed by this rule


class AutoOut(BaseModel):
    batch_id: str | None
    asked: int  # payees sent to the model this time
    applied: list[AppliedOut]
    transactions: int  # covered by the applied rules
    left: int  # answered but not confident enough: for the user
    called: bool  # False: nothing new to ask, no request made


class RemovedOut(BaseModel):
    removed: int


def _auto_out(batch_id: str | None, asked: int, applied: list[auto.Applied], left: int, called: bool) -> AutoOut:
    return AutoOut(batch_id=batch_id, asked=asked, applied=[AppliedOut(**vars(a)) for a in applied],
                   transactions=sum(a.transactions for a in applied), left=left, called=called)


@router.post("/auto-categorize", response_model=AutoOut)
def auto_categorize(request: Request, user: CurrentUser = Depends(current_user),
                    db: Database = Depends(get_db)) -> AutoOut:
    with db.read_session() as s:
        plan: auto.Plan = auto.plan(s, user.id)
        if not plan.candidates:  # clicking again costs nothing and changes nothing
            return _auto_out(None, 0, [], auto.declined(s, user.id), called=False)
    try:  # outside the writer lock: a model call takes seconds
        answers: dict[str, tuple[str | None, int]] = auto.ask(
            request.app.state.settings, plan, request.app.state.llm_transport, request.app.state.llm_sleep)
    except (LLMUnavailable, ContractError) as exc:
        _log.warning("auto-categorize for user %s failed: %s", user.id, exc)  # the person gets plain words
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Auto-categorize isn't available right now; nothing was changed. Try again later.") from exc
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        batch_id, applied = auto.store(s, user.id, user.role == "admin", plan, answers)
    return _auto_out(batch_id, len(plan.candidates), applied, len(answers) - len(applied), called=True)


@router.get("/auto-categorize/latest", response_model=AutoOut)
def latest_batch(user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)) -> AutoOut:
    """The last batch's rules that are still the model's (a rule the user changed is theirs now)."""
    with db.read_session() as s:
        batch_id: str | None = s.scalars(select(PayeeCategory.batch_id).where(
            PayeeCategory.user_id == user.id, PayeeCategory.origin == "llm")
            .order_by(PayeeCategory.updated_at.desc()).limit(1)).first()
        applied: list[auto.Applied] = []
        if batch_id is not None:
            for rule, suggestion, payee in s.execute(
                    select(PayeeCategory, PayeeSuggestion, Payee)
                    .outerjoin(PayeeSuggestion, and_(PayeeSuggestion.user_id == PayeeCategory.user_id,
                                                     PayeeSuggestion.payee_id == PayeeCategory.payee_id))
                    .join(Payee, Payee.id == PayeeCategory.payee_id)
                    .where(PayeeCategory.user_id == user.id, PayeeCategory.batch_id == batch_id,
                           PayeeCategory.origin == "llm").order_by(Payee.normalized_name)).tuples():
                count: int = s.scalar(select(func.count()).select_from(TransactionEffective).where(
                    TransactionEffective.user_id == user.id, TransactionEffective.payee_id == rule.payee_id,
                    TransactionEffective.kind.in_(CATEGORIZABLE),
                    TransactionEffective.category_override_id.is_(None))) or 0
                applied.append(auto.Applied(rule.payee_id, payee.normalized_name, rule.category_id,
                                            suggestion.confidence_percent if suggestion else 0, count))
        return _auto_out(batch_id, 0, applied, auto.declined(s, user.id), called=False)


@router.delete("/auto-categorize/{batch_id}", response_model=RemovedOut)
def undo_batch(batch_id: str, request: Request, user: CurrentUser = Depends(current_user),
               db: Database = Depends(get_db)) -> RemovedOut:
    """Undo a batch: its rules go, and those transactions read uncategorized again. Nothing else is touched:
    read-time resolution means no transaction was ever written."""
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        gone: list[str] = list(s.scalars(delete(PayeeCategory).where(
            PayeeCategory.user_id == user.id, PayeeCategory.batch_id == batch_id, PayeeCategory.origin == "llm")
            .returning(PayeeCategory.payee_id)))
        # Forget the batch's answers too, so the next click asks afresh (user, 2026-10-19: after an undo,
        # remembered answers made every later click a silent no-op).
        for memory in (PayeeSuggestion, RawPayeeSuggestion):
            s.execute(delete(memory).where(memory.user_id == user.id, memory.batch_id == batch_id))
    return RemovedOut(removed=len(gone))
