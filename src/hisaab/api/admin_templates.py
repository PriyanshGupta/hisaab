"""/api/admin/templates: the template promotion queue (PRD §6.8; tech-design §4.10 step 4). Admin only.

GET lists every LLM-proposed candidate still waiting (passing or discarded) with what review needs side by
side: the format, the spec, and per example the text the patterns read (while the email is still stored)
beside the field-by-field replay table. Then the demoted templates, and the formats excluded as
non-transactional (their emails skip the model). Replay proves consistency, not correctness, so the raw
text is the point: the admin checks the values against the email itself.

POST .../approve runs promote.approve in one write (409 unless replay passed); POST .../reject discards;
POST /formats/{id}/include sends an excluded format's emails back to the model.
Members get 403 and anonymous callers 401 before any handler runs (require_admin).
"""

import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from hisaab.api.deps import get_db, require_admin
from hisaab.db import Database
from hisaab.models import FormatSighting, ParserTemplate, Source
from hisaab.parsing.email_interpreter import EmailArtifact, parse_eml
from hisaab.parsing.transforms import collapse_whitespace, strip_html
from hisaab.promotion import promote
from hisaab.promotion.sightings import include_again

router: APIRouter = APIRouter(prefix="/api/admin/templates", tags=["admin"], dependencies=[Depends(require_admin)])


class ReplayField(BaseModel):
    field: str
    expected: Any
    got: Any
    match: bool


class CandidateExample(BaseModel):
    source_id: str
    subject: str | None  # None once the email is no longer stored
    received_at: str | None
    text: str | None  # the text the patterns read
    fields: list[ReplayField]


class Candidate(BaseModel):
    id: str
    name: str
    status: str  # validated (approvable) or proposed (discarded)
    passed: bool
    problems: list[str]
    sender: str
    subject_masked: str
    sightings: int
    first_seen_at: str
    last_seen_at: str
    spec: dict[str, Any]
    examples: list[CandidateExample]


class Demoted(BaseModel):
    id: str
    name: str
    demoted_at: str | None
    consecutive_failures: int


class Excluded(BaseModel):
    id: str  # the format (sighting), not a template
    sender: str
    subject_masked: str
    sightings: int
    last_seen_at: str


class Queue(BaseModel):
    candidates: list[Candidate]
    demoted: list[Demoted]
    excluded: list[Excluded]


class Approved(BaseModel):
    reread: int
    waiting: int
    invalidated: int
    exported: list[str]
    flagged: int


@router.get("")
def queue(db: Database = Depends(get_db)) -> Queue:
    with db.read_session() as s:
        rows: list[tuple[ParserTemplate, FormatSighting]] = list(s.execute(
            select(ParserTemplate, FormatSighting).join(FormatSighting, FormatSighting.template_id == ParserTemplate.id)
            .where(ParserTemplate.origin == "llm_proposed", ParserTemplate.status.in_(("proposed", "validated")))
            .order_by(ParserTemplate.created_at.desc())).tuples())
        demoted: list[ParserTemplate] = list(s.scalars(select(ParserTemplate).where(
            ParserTemplate.status == "demoted").order_by(ParserTemplate.demoted_at.desc())))
        excluded: list[FormatSighting] = list(s.scalars(select(FormatSighting).where(
            FormatSighting.state == "excluded_non_transactional").order_by(FormatSighting.last_seen_at.desc())))
        return Queue(candidates=[_candidate(s, t, f) for t, f in rows],
                     demoted=[Demoted(id=t.id, name=t.name, demoted_at=t.demoted_at,
                                      consecutive_failures=t.consecutive_failures) for t in demoted],
                     excluded=[Excluded(id=x.id, sender=x.sender_address, subject_masked=x.subject_masked,
                                        sightings=x.sighting_count, last_seen_at=x.last_seen_at) for x in excluded])


def _candidate(session: Session, template: ParserTemplate, sighting: FormatSighting) -> Candidate:
    validation: dict[str, Any] = json.loads(template.validation_result or "{}")
    replayed: dict[str, list[dict[str, Any]]] = {e["source_id"]: e["fields"] for e in validation.get("examples", [])}
    examples: list[CandidateExample] = []
    for source_id in json.loads(template.example_source_ids or "[]"):
        source: Source | None = session.get(Source, source_id)
        artifact: EmailArtifact | None = parse_eml(source.content) if source and source.content else None
        examples.append(CandidateExample(
            source_id=source_id, subject=artifact.subject if artifact else None,
            received_at=source.received_at if source else None,
            text=collapse_whitespace(strip_html(artifact.body)) if artifact else None,
            fields=[ReplayField(**r) for r in replayed.get(source_id, [])]))
    return Candidate(id=template.id, name=template.name, status=template.status,
                     passed=bool(validation.get("passed")), problems=validation.get("problems", []),
                     sender=sighting.sender_address, subject_masked=sighting.subject_masked,
                     sightings=sighting.sighting_count, first_seen_at=sighting.first_seen_at,
                     last_seen_at=sighting.last_seen_at, spec=json.loads(template.spec), examples=examples)


@router.post("/{template_id}/approve")
def approve(template_id: str, request: Request, db: Database = Depends(get_db)) -> Approved:
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        result: promote.ApproveResult = promote.approve(s, template_id, request.app.state.settings)
    return Approved(reread=result.reread, waiting=result.waiting, invalidated=result.invalidated,
                    exported=result.exported, flagged=result.flagged)


@router.post("/formats/{sighting_id}/include", status_code=status.HTTP_204_NO_CONTENT)
def include(sighting_id: str, request: Request, db: Database = Depends(get_db)) -> Response:
    """A format excluded as non-transactional that isn't: its emails go back to the model."""
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        include_again(s, sighting_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{template_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
def reject(template_id: str, request: Request, db: Database = Depends(get_db)) -> Response:
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        promote.reject(s, template_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
