"""/api/admin/senders: the sender review queue (PRD §6.10; tech-design §5.3). Admin only.

GET lists every sender the discovery sweep found that waits for review, once, with each inbox it was found in
(by that person's name), how many matching emails the sweep read there, and its examples (subject, Gmail's
short preview, date: never a body). Then the ignored senders and when the last sweep finished.

POST .../approve activates the sender for everyone and queues its three-month backfill for every connected
inbox; POST .../ignore keeps it out of future sweeps; POST /sweep runs the sweep now for every connected inbox.
Members get 403 and anonymous callers 401 before any handler runs (require_admin).
"""

import json
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import func, select

from hisaab.api.deps import get_db, require_admin
from hisaab.db import Database
from hisaab.ingest.sweep import enqueue_sweeps
from hisaab.models import Job, Sender, SenderCandidate, User
from hisaab.senders import review

router: APIRouter = APIRouter(prefix="/api/admin/senders", tags=["admin"], dependencies=[Depends(require_admin)])


class SweepExample(BaseModel):
    subject: str
    snippet: str
    received_at: str


class FoundIn(BaseModel):
    user: str  # the inbox owner's display name
    match_count: int
    examples: list[SweepExample]


class CandidateSender(BaseModel):
    id: str
    address: str
    first_seen_at: str
    found_in: list[FoundIn]


class IgnoredSender(BaseModel):
    id: str
    address: str
    reviewed_at: str | None


class SenderQueue(BaseModel):
    candidates: list[CandidateSender]
    ignored: list[IgnoredSender]
    last_sweep_at: str | None


class SenderApproved(BaseModel):
    address: str
    backfills: int  # inboxes whose three months of this sender's mail is being fetched


class SweepQueued(BaseModel):
    jobs: list[str]


@router.get("")
def senders(db: Database = Depends(get_db)) -> SenderQueue:
    with db.read_session() as s:
        waiting: list[Sender] = list(s.scalars(select(Sender).where(Sender.status == "candidate")
                                               .order_by(Sender.email_address)))
        rows: list[tuple[SenderCandidate, str]] = list(s.execute(
            select(SenderCandidate, User.display_name).join(User, User.id == SenderCandidate.discovered_by_user_id)
            .where(SenderCandidate.discarded_at.is_(None)).order_by(User.display_name)).tuples())
        by_address: defaultdict[str, list[tuple[SenderCandidate, str]]] = defaultdict(list)
        for candidate, name in rows:
            by_address[candidate.sender_address].append((candidate, name))
        ignored: list[Sender] = list(s.scalars(select(Sender).where(Sender.status == "ignored")
                                               .order_by(Sender.reviewed_at.desc())))
        last: str | None = s.scalar(select(func.max(Job.finished_at)).where(Job.kind == "sweep",
                                                                           Job.state == "succeeded"))
        return SenderQueue(
            candidates=[_candidate(sender, by_address[sender.email_address]) for sender in waiting
                        if by_address[sender.email_address]],
            ignored=[IgnoredSender(id=x.id, address=x.email_address, reviewed_at=x.reviewed_at) for x in ignored],
            last_sweep_at=last)


def _candidate(sender: Sender, found: list[tuple[SenderCandidate, str]]) -> CandidateSender:
    def examples(candidate: SenderCandidate) -> list[SweepExample]:
        raw: Any = json.loads(candidate.matched_subjects or "[]")
        return [SweepExample(**e) for e in raw]
    return CandidateSender(id=sender.id, address=sender.email_address,
                           first_seen_at=min(c.first_seen_at for c, _ in found),
                           found_in=[FoundIn(user=name, match_count=c.match_count, examples=examples(c))
                                     for c, name in found])


@router.post("/{sender_id}/approve")
def approve(sender_id: str, request: Request, db: Database = Depends(get_db)) -> SenderApproved:
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        address: str = review.approve(s, sender_id)
    jobs: list[str] = review.enqueue_backfills(request.app.state.jobs, address)  # after commit: its own write
    return SenderApproved(address=address, backfills=len(jobs))


@router.post("/{sender_id}/ignore", status_code=status.HTTP_204_NO_CONTENT)
def ignore(sender_id: str, request: Request, db: Database = Depends(get_db)) -> Response:
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        review.ignore(s, sender_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sweep", status_code=status.HTTP_202_ACCEPTED)
def sweep_now(request: Request) -> SweepQueued:
    return SweepQueued(jobs=enqueue_sweeps(request.app.state.jobs))
