"""Reading endpoints for the report screen. Read-only; nothing here is stored (tech-design §8.3, §8.5).

    GET /api/report      one period's report (slice 18; reports/spending.py)
    GET /api/trends      per-category spending for the 12 months ending this month (slice 21; reports/trends.py)
    GET /api/recurring   recurring charges among the last 12 months' expenses (slice 21; reports/recurring.py)
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from hisaab.api.deps import CurrentUser, current_user, get_db
from hisaab.db import Database
from hisaab.models import Category
from hisaab.reports.recurring import Series, recurring
from hisaab.reports.spending import ReportOut, report
from hisaab.reports.trends import Trends, trends

router: APIRouter = APIRouter(tags=["report"])


class TrendCategory(BaseModel):
    category_id: str | None  # None: not categorized yet
    name: str
    months: list[int | None]  # paise per month, oldest first; None: before the history starts
    total_paise: int


class TrendsOut(BaseModel):
    months: list[str]  # "YYYY-MM", oldest first, ending this month
    history_from: str | None  # the earliest transaction date; None: no data yet
    partial: list[str]  # months only partly covered
    categories: list[TrendCategory]


class RecurringOut(BaseModel):
    payee: str
    category: str | None
    account: str | None
    count: int
    typical_gap_days: int
    latest_paise: int
    first_paise: int
    last_on: str
    next_on: str  # when the next charge is due: the last one plus the typical gap
    yearly_paise: int  # about a year's cost at the latest amount


@router.get("/api/report", response_model=ReportOut)
def get_report(start: date, end: date, user: CurrentUser = Depends(current_user),
               db: Database = Depends(get_db)) -> ReportOut:
    """Both ends inclusive, by transaction date."""
    if start > end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The period starts after it ends.")
    with db.read_session() as s:
        return report(s, user.id, start.isoformat(), end.isoformat())


@router.get("/api/trends", response_model=TrendsOut)
def get_trends(user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)) -> TrendsOut:
    with db.read_session() as s:
        t: Trends = trends(s, user.id)
    return TrendsOut(months=t.months, history_from=t.history_from, partial=t.partial, categories=[
        TrendCategory(category_id=c.category_id, name=c.name, months=c.months, total_paise=c.total)
        for c in t.categories])


@router.get("/api/recurring", response_model=list[RecurringOut])
def get_recurring(user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)) -> list[RecurringOut]:
    with db.read_session() as s:
        found: list[Series] = recurring(s, user.id)
        names: dict[str, str] = dict(s.execute(select(Category.id, Category.name).where(
            Category.user_id == user.id)).tuples().all())
    return [RecurringOut(payee=r.payee_name, category=names.get(r.category_id) if r.category_id else None,
                         account=r.account_label, count=len(r.charges), typical_gap_days=r.typical_gap_days,
                         latest_paise=r.latest_amount, first_paise=r.first_amount, last_on=r.last_on,
                         next_on=r.next_on, yearly_paise=r.yearly_paise) for r in found]
