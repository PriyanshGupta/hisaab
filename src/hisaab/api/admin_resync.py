"""/api/admin/resync: re-sync a date range, and the parser-change notices that suggest one (tech-design §5.2).

GET /preview names what a re-sync of that range would touch (transactions and inboxes), so the confirmation can
say it before anything runs. POST takes a backup first (§9.2; no backup, no re-sync), then queues one resync
job per connected inbox (or the one asked for); a re-sync of
every inbox resolves the notices whose range it covers (one inbox's doesn't: templates are global). GET /notices lists the open parser-change notices for the admin banner,
newest first; POST /notices/{id}/dismiss closes one without re-syncing. Admin only (require_admin).
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select

from hisaab.api.deps import get_db, require_admin
from hisaab.config import Settings
from hisaab.db import Database
from hisaab.ingest.resync import affected, enqueue_resync, inboxes, resolve_notices
from hisaab.models import ResyncNotice, utc_now
from hisaab.ops.backup import BackupFailed, backup

router: APIRouter = APIRouter(prefix="/api/admin/resync", tags=["admin"], dependencies=[Depends(require_admin)])


class ResyncPreview(BaseModel):
    transactions: int  # how many may change
    inboxes: int


class ResyncIn(BaseModel):
    start: str  # India date, YYYY-MM-DD
    end: str
    user_id: str | None = None  # one inbox, or every connected one


class ResyncQueued(BaseModel):
    jobs: list[str]


class ResyncNoticeOut(BaseModel):
    id: str
    template_name: str
    reason: str  # edited | approved | shipped_updated
    range_from: str
    range_to: str
    created_at: str


def _range(start: str, end: str) -> None:
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Dates must look like 2026-10-05.") from None
    if last < first:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The range ends before it starts.")


@router.get("/preview")
def preview(start: str = "", end: str = "", user_id: str | None = None,
            db: Database = Depends(get_db)) -> ResyncPreview:
    _range(start, end)
    with db.read_session() as s:
        return ResyncPreview(transactions=affected(s, start, end, user_id), inboxes=len(inboxes(s, user_id)))


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def resync(body: ResyncIn, request: Request, db: Database = Depends(get_db)) -> ResyncQueued:
    _range(body.start, body.end)
    with db.read_session() as s:
        targets: list[str] = inboxes(s, body.user_id)
    if targets:  # tech-design §9.2: a backup before every re-sync; without one, nothing is re-synced
        settings: Settings = request.app.state.settings
        try:
            backup(settings.database_path, settings.backup_dir, "pre-resync")
        except BackupFailed as e:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                                f"Couldn't take the backup first, so nothing was re-synced: {e}.") from None
    jobs: list[str] = enqueue_resync(request.app.state.jobs, body.start, body.end, body.user_id)
    if jobs and body.user_id is None:  # templates are global: one inbox's re-sync doesn't cover a notice
        with db.write_session(timeout=request.app.state.writer_timeout) as s:
            resolve_notices(s, body.start, body.end)
    return ResyncQueued(jobs=jobs)


@router.get("/notices")
def notices(db: Database = Depends(get_db)) -> list[ResyncNoticeOut]:
    with db.read_session() as s:
        return [ResyncNoticeOut(id=n.id, template_name=n.template_name, reason=n.reason, range_from=n.range_from,
                                range_to=n.range_to, created_at=n.created_at)
                for n in s.scalars(select(ResyncNotice).where(ResyncNotice.resolved_at.is_(None))
                                   .order_by(ResyncNotice.created_at.desc()))]


@router.post("/notices/{notice_id}/dismiss", status_code=status.HTTP_204_NO_CONTENT)
def dismiss(notice_id: str, request: Request, db: Database = Depends(get_db)) -> Response:
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        notice: ResyncNotice | None = s.get(ResyncNotice, notice_id)
        if notice is None or notice.resolved_at is not None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such open notice.")
        notice.resolved_at = utc_now()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
