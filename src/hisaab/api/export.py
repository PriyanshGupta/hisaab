"""/api/export/transactions.csv: the signed-in person's transactions for a period, as CSV (tech-design §9.7).

Any signed-in user, their own rows only. The file opens in a spreadsheet: UTF-8 with a byte-order mark (so Excel
reads non-ASCII names right), every field quoted as CSV needs, named hisaab-2026-10.csv for a whole month or
hisaab-2026-10-01_2026-10-15.csv otherwise.
"""

import calendar
import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Response, status

from hisaab.api.deps import CurrentUser, current_user, get_db
from hisaab.db import Database
from hisaab.reports.export import COLUMNS, export_rows

router: APIRouter = APIRouter(prefix="/api/export", tags=["export"])


def _filename(first: date, last: date) -> str:
    whole_month: bool = (first.day == 1 and last.year == first.year and last.month == first.month
                         and last.day == calendar.monthrange(first.year, first.month)[1])
    return f"hisaab-{first:%Y-%m}.csv" if whole_month else f"hisaab-{first.isoformat()}_{last.isoformat()}.csv"


@router.get("/transactions.csv")
def transactions_csv(start: str = "", end: str = "", user: CurrentUser = Depends(current_user),
                     db: Database = Depends(get_db)) -> Response:
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Dates must look like 2026-10-01.") from None
    if last < first:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The period ends before it starts.")
    with db.read_session() as s:
        rows: list[dict[str, str]] = export_rows(s, user.id, start, end)
    out: io.StringIO = io.StringIO()
    writer: csv.DictWriter[str] = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return Response(content="﻿" + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{_filename(first, last)}"'})
