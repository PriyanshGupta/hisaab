"""/api/jobs/{id}: poll a job a request started (tech-design §6.7). Users see only their own jobs."""

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from hisaab.api.deps import CurrentUser, current_user, get_db
from hisaab.db import Database
from hisaab.models import Job

router: APIRouter = APIRouter(prefix="/api/jobs", tags=["jobs"])


class JobOut(BaseModel):
    id: str
    kind: str
    state: str  # queued | running | succeeded | failed | cancelled
    result: dict[str, Any] | None
    error: str | None  # a sentence for the person
    created_at: str
    started_at: str | None
    finished_at: str | None


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str, user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)) -> JobOut:
    with db.read_session() as s:
        job: Job | None = s.get(Job, job_id)
        if job is None or job.user_id != user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such job")
        return JobOut(id=job.id, kind=job.kind, state=job.state, result=json.loads(job.result) if job.result else None,
                      error=json.loads(job.error)["message"] if job.error else None, created_at=job.created_at,
                      started_at=job.started_at, finished_at=job.finished_at)
