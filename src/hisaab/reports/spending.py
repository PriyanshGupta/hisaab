"""The report (tech-design §8; PRD §7): one period's spending by category, its totals, every transfer.

- Breakdown: expenses minus refunds, grouped by the read-time category (§6.5), then payee, then
  transaction. A refund nets against its own resolved category (its override, else its payee's
  rule): nothing links a refund to its original yet. It never depends on the Spending/Cash out
  toggle (§8.2); only the totals do, and both are returned.
- Spending = the breakdown's sum, uncategorized included. Cash out (PRD §3.5) = expenses paid from a
  bank or cash account, plus card bills paid, minus refunds into a bank or cash account. Money
  whose account isn't known yet (spending from a number not assigned, a card bill whose payer is still
  a review question) fits neither side; cash_out_unknown says how much. A number the user marked "not my
  account" (a FASTag wallet) is outside their accounts for good: in neither, and nothing to ask.
- Transfers are listed and counted in neither spending nor money in. Reversed payments are listed
  and counted nowhere. Adjustments (a balance gap the user accepted, slice 20) are deliberately outside
  every total: they are visible in the transaction list, never spending, cash out or money in. Lending
  is schema only.
"""

from collections.abc import Iterable
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from hisaab.accounts.identifiers import normalize_for_lookup
from hisaab.models import Account, Category, ExternalIdentifier
from hisaab.models import TransactionEffective as T
from hisaab.transactions.repository import newest_first, with_provenance

Provenance: TypeAlias = Literal["email", "statement", "manual"]
TransferKind: TypeAlias = Literal["card_bill", "cash_withdrawal", "between_accounts"]
LIQUID: frozenset[str] = frozenset({"bank", "cash"})  # account types whose outflow is cash out
CashSide: TypeAlias = Literal["cash", "unknown"] | None
UNCATEGORIZED: str = "Not categorized yet"


class ReportTransaction(BaseModel):
    id: str
    transaction_date: str
    amount_paise: int  # signed as it counts: a refund is negative in the breakdown
    kind: str
    account_label: str | None  # the account's name, "ending 4821" if not assigned yet, None if no account
    payee_raw: str | None
    provenance: Provenance
    needs_review: bool


class ReportPayee(BaseModel):
    key: str  # payee id, "raw:<text>" for a payee not named yet, "" when the source named none
    name: str
    amount_paise: int
    transactions: list[ReportTransaction]  # newest first


class ReportCategory(BaseModel):
    category_id: str | None  # None: not categorized; always present, always last
    name: str
    amount_paise: int  # expenses minus refunds
    count: int
    payees: list[ReportPayee]  # biggest first


class ReportTransfer(BaseModel):
    id: str
    transaction_date: str
    amount_paise: int
    kind: TransferKind
    from_label: str | None
    to_label: str | None


class ReportOut(BaseModel):
    start: str
    end: str
    spending_paise: int  # the breakdown's sum, uncategorized included
    cash_out_paise: int
    cash_out_unknown_paise: int  # money whose account isn't known yet: in neither cash figure
    money_in_paise: int
    categories: list[ReportCategory]  # biggest first; uncategorized last, even at zero
    income: list[ReportPayee]
    transfers: list[ReportTransfer]  # oldest first
    reversed: list[ReportTransaction]  # failed payments: shown, never counted


def _label(accounts: dict[str, Account], account_id: str | None, unresolved: str | None) -> str | None:
    if account_id is not None:
        return accounts[account_id].display_name
    if unresolved:
        return f"ending {unresolved}" if unresolved.isdigit() else unresolved
    return None


def _cash_side(accounts: dict[str, Account], not_mine: set[str], account_id: str | None,
               unresolved: str | None) -> CashSide:
    """Where money through this leg lands: cash out, not known yet, or neither (a card, or not theirs)."""
    if account_id is not None:
        return "cash" if accounts[account_id].type in LIQUID else None
    canonical: str | None = normalize_for_lookup(unresolved) if unresolved else None
    return None if canonical in not_mine else "unknown"


def _own_leg(t: T) -> tuple[str | None, str | None]:
    """The user's side of a payment: the account money left (expense) or reached (refund, income)."""
    return ((t.from_account_id, t.unresolved_from_identifier) if t.kind == "expense"
            else (t.to_account_id, t.unresolved_to_identifier))


def _leaf(row: Any, accounts: dict[str, Account], amount: int) -> ReportTransaction:
    t: T = row.TransactionEffective
    return ReportTransaction(id=t.id, transaction_date=t.transaction_date, amount_paise=amount, kind=t.kind,
                             account_label=_label(accounts, *_own_leg(t)), payee_raw=t.payee_raw,
                             provenance=row.provenance, needs_review=t.status == "needs_review")


def _add(payees: dict[str, ReportPayee], row: Any, leaf: ReportTransaction) -> None:
    t: T = row.TransactionEffective
    key: str = t.payee_id or (f"raw:{t.payee_raw}" if t.payee_raw else "")
    payee: ReportPayee = payees.setdefault(key, ReportPayee(
        key=key, name=row.payee_name or t.payee_raw or "Unknown payee", amount_paise=0, transactions=[]))
    payee.amount_paise += leaf.amount_paise
    payee.transactions.append(leaf)


def _ranked(payees: Iterable[ReportPayee]) -> list[ReportPayee]:
    return sorted(payees, key=lambda p: (-p.amount_paise, p.name))


def _category(category_id: str | None, name: str, payees: dict[str, ReportPayee]) -> ReportCategory:
    ranked: list[ReportPayee] = _ranked(payees.values())
    return ReportCategory(category_id=category_id, name=name, amount_paise=sum(p.amount_paise for p in ranked),
                          count=sum(len(p.transactions) for p in ranked), payees=ranked)


def _transfer_kind(t: T, accounts: dict[str, Account]) -> TransferKind:
    to_type: str | None = accounts[t.to_account_id].type if t.to_account_id else None
    from_type: str | None = accounts[t.from_account_id].type if t.from_account_id else None
    if to_type == "credit_card" and from_type != "credit_card":
        return "card_bill"
    if to_type == "cash" and from_type == "bank":
        return "cash_withdrawal"
    return "between_accounts"


def report(session: Session, user_id: str, start: str, end: str) -> ReportOut:
    rows: list[Any] = list(session.execute(with_provenance(user_id).where(
        T.transaction_date >= start, T.transaction_date <= end).order_by(*newest_first(T), T.id)))
    accounts: dict[str, Account] = {a.id: a for a in session.scalars(select(Account).where(Account.user_id == user_id))}
    names: dict[str, str] = dict(session.execute(select(Category.id, Category.name).where(
        Category.user_id == user_id)).tuples().all())
    not_mine: set[str] = set(session.scalars(select(ExternalIdentifier.identifier).where(
        ExternalIdentifier.user_id == user_id)))
    totals: dict[CashSide, int] = {"cash": 0, "unknown": 0, None: 0}
    groups: dict[str | None, dict[str, ReportPayee]] = {}
    income: dict[str, ReportPayee] = {}
    transfers: list[ReportTransfer] = []
    reversed_: list[ReportTransaction] = []
    for row in rows:
        t: T = row.TransactionEffective
        if t.status == "reversed":
            reversed_.append(_leaf(row, accounts, t.amount))
        elif t.kind in ("expense", "refund"):
            signed: int = t.amount if t.kind == "expense" else -t.amount
            _add(groups.setdefault(row.category_id, {}), row, _leaf(row, accounts, signed))
            totals[_cash_side(accounts, not_mine, *_own_leg(t))] += signed
        elif t.kind == "income":
            _add(income, row, _leaf(row, accounts, t.amount))
        elif t.kind == "transfer":
            kind: TransferKind = _transfer_kind(t, accounts)
            if kind == "card_bill":  # the one transfer that is cash out: the bill left a bank (PRD §3.5)
                totals[_cash_side(accounts, not_mine, t.from_account_id, t.unresolved_from_identifier)] += t.amount
            transfers.append(ReportTransfer(
                id=t.id, transaction_date=t.transaction_date, amount_paise=t.amount, kind=kind,
                from_label=_label(accounts, t.from_account_id, t.unresolved_from_identifier),
                to_label=_label(accounts, t.to_account_id, t.unresolved_to_identifier)))
    named: list[ReportCategory] = sorted(
        (_category(cid, names[cid], payees) for cid, payees in groups.items() if cid is not None),
        key=lambda c: (-c.amount_paise, c.name))
    categories: list[ReportCategory] = [*named, _category(None, UNCATEGORIZED, groups.get(None, {}))]
    return ReportOut(
        start=start, end=end, spending_paise=sum(c.amount_paise for c in categories),
        cash_out_paise=totals["cash"], cash_out_unknown_paise=totals["unknown"], money_in_paise=sum(p.amount_paise for p in income.values()),
        categories=categories, income=_ranked(income.values()), transfers=transfers[::-1], reversed=reversed_)
