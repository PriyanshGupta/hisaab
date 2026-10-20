"""Per-category trends for the last 12 months (PRD §7.7; tech-design §8.3; rulings 2026-10-20).

The 12 India months ending this one, always by transaction_date. Spending per category per month is the
report's own measure: expenses minus refunds, the category resolved at read time, reversed payments
counted nowhere (reports/spending.py). A month before the user's history starts is None, so a chart draws
nothing there rather than a fake zero; inside the history, a month with no spending is 0. The month the
history starts in (unless it starts on the 1st) and the running month are partial.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hisaab.clock import local_today
from hisaab.models import Category
from hisaab.models import TransactionEffective as T
from hisaab.reports.spending import UNCATEGORIZED
from hisaab.transactions.repository import with_provenance

MONTHS: int = 12


@dataclass(frozen=True)
class CategoryTrend:
    category_id: str | None  # None: not categorized yet
    name: str
    months: list[int | None]  # paise per month, oldest first; None before the history starts
    total: int


@dataclass(frozen=True)
class Trends:
    months: list[str]  # "YYYY-MM", oldest first, ending this India month
    history_from: str | None  # the user's earliest transaction date; None: nothing yet
    partial: list[str]  # months only partly covered: the history's first, and the running one
    categories: list[CategoryTrend]  # by total, biggest first; not categorized last; never-spent left out


def _months(today: date) -> list[str]:
    year, month = today.year, today.month
    out: list[str] = []
    for _ in range(MONTHS):
        out.append(f"{year:04d}-{month:02d}")
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return out[::-1]


def trends(session: Session, user_id: str, today: date | None = None) -> Trends:
    today = today or local_today()
    months: list[str] = _months(today)
    history_from: str | None = session.scalar(select(func.min(T.transaction_date)).where(  # through the edits
        T.user_id == user_id))
    partial: list[str] = sorted({months[-1]} | (
        {history_from[:7]} if history_from and not history_from.endswith("-01") and history_from[:7] in months
        else set()))
    rows: list[Any] = list(session.execute(with_provenance(user_id).where(
        T.kind.in_(("expense", "refund")), T.status != "reversed",
        T.transaction_date >= f"{months[0]}-01", T.transaction_date <= today.isoformat())))
    sums: dict[str | None, dict[str, int]] = {}
    for row in rows:
        t: T = row.TransactionEffective
        signed: int = t.amount if t.kind == "expense" else -t.amount
        per_month: dict[str, int] = sums.setdefault(row.category_id, {})
        per_month[t.transaction_date[:7]] = per_month.get(t.transaction_date[:7], 0) + signed
    names: dict[str, str] = dict(session.execute(select(Category.id, Category.name).where(
        Category.user_id == user_id)).tuples().all())
    first: str | None = history_from[:7] if history_from else None
    out: list[CategoryTrend] = []
    for category_id, per_month in sums.items():
        values: list[int | None] = [None if first is None or m < first else per_month.get(m, 0) for m in months]
        if not any(values):  # nothing spent, or refunds and spending cancelling month by month: no chart
            continue
        out.append(CategoryTrend(category_id, names.get(category_id, UNCATEGORIZED) if category_id else UNCATEGORIZED,
                                 values, sum(v or 0 for v in values)))
    out.sort(key=lambda c: (c.category_id is None, -c.total, c.name))
    return Trends(months, history_from, partial, out)
