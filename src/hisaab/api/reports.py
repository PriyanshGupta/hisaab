"""/api/report (slice 18): one period's report. Read-only; see reports/spending.py for what counts where."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status

from hisaab.api.deps import CurrentUser, current_user, get_db
from hisaab.db import Database
from hisaab.reports.spending import ReportOut, report

router: APIRouter = APIRouter(prefix="/api/report", tags=["report"])


@router.get("", response_model=ReportOut)
def get_report(start: date, end: date, user: CurrentUser = Depends(current_user),
               db: Database = Depends(get_db)) -> ReportOut:
    """Both ends inclusive, by transaction date."""
    if start > end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The period starts after it ends.")
    with db.read_session() as s:
        return report(s, user.id, start.isoformat(), end.isoformat())
