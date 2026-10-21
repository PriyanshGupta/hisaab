"""CSV export, per user per period (tech-design §9.7).

The rows are the signed-in person's own transactions in the date range, as the app shows them now: read through
transactions_effective (edits applied) and categorized() (categories resolved by today's rules). So an export is
a snapshot of current rules, not a fixed record: two exports a month apart can legitimately disagree.

Amounts are plain rupees with two decimals, formatted from integer paise (never a float). An account the alert
named but which isn't one of the user's yet shows the identifier as printed.
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from hisaab.categories.resolution import categorized
from hisaab.models import Account, Category
from hisaab.models import TransactionEffective as T

COLUMNS: tuple[str, ...] = ("Date", "Time", "Type", "Amount", "From account", "To account", "Payee",
                            "Text from the bank", "Category", "Status", "Reference")


def rupees(paise: int) -> str:
    sign: str = "-" if paise < 0 else ""
    whole, part = divmod(abs(paise), 100)
    return f"{sign}{whole}.{part:02d}"


def export_rows(session: Session, user_id: str, start: str, end: str) -> list[dict[str, str]]:
    date.fromisoformat(start), date.fromisoformat(end)  # the caller validated; a bad date fails loudly here too
    accounts: dict[str, str] = dict(session.execute(
        select(Account.id, Account.display_name).where(Account.user_id == user_id)).tuples().all())
    categories: dict[str, str] = dict(session.execute(
        select(Category.id, Category.name).where(Category.user_id == user_id)).tuples().all())
    rows: list[dict[str, str]] = []
    for t, category_id, _source, payee_name in session.execute(
            categorized(user_id).where(T.transaction_date >= start, T.transaction_date <= end)
            .order_by(T.transaction_date, T.transaction_time.nulls_first(), T.id)).tuples():
        rows.append(dict(zip(COLUMNS, (
            t.transaction_date, (t.transaction_time or "")[:5], t.kind, rupees(t.amount),
            accounts.get(t.from_account_id or "", t.unresolved_from_identifier or ""),
            accounts.get(t.to_account_id or "", t.unresolved_to_identifier or ""),
            payee_name or "", t.payee_raw or "", categories.get(category_id or "", ""), t.status,
            t.reference_number or "",
        ))))
    return rows
