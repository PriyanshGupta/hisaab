"""/api/statements: statement upload (tech-design §7, PRD §4.6, §6.4).

    POST   /api/statements             read the PDF with its password, stage it, return the preview
    GET    /api/statements             the user's open previews
    GET    /api/statements/{id}        one preview: new rows, matches, rows that couldn't be read, diagnostic
    PATCH  /api/statements/{id}        pick the account when the header didn't bind one
    DELETE /api/statements/{id}        discard
    POST   /api/statements/{id}/merge  enqueue the statement_merge job

The upload is JSON with the file as base64, so no multipart dependency is
needed for one small PDF. The password is a local variable of the POST handler
only: never stored, logged, put in job params or echoed in an error (PRD §4.6).
Reading the PDF happens on the read pool, outside the writer lock; staging is
one short write (ruling 2026-10-10). An expired preview reads as gone (404).
"""

import base64
import binascii
import hashlib
import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from hisaab.api.deps import CurrentUser, current_user, get_db
from hisaab.api.gmail import JobRef
from hisaab.db import Database
from hisaab.jobs.queue import JobQueue
from hisaab.models import Account, ParserTemplate, StatementStaging, utc_now
from hisaab.parsing.statement_interpreter import StatementRow
from hisaab.statements.reading import ReadStatement, StatementError, read_statement
from hisaab.statements.staging import account_kind, load, rebind, rows_of, stage
from hisaab.transactions.repository import ListedTransaction, list_by_ids

router: APIRouter = APIRouter(prefix="/api/statements", tags=["statements"])

MAX_PDF_BYTES: int = 10 * 1024 * 1024
MAX_PASSWORD: int = 200


class Upload(BaseModel):
    # Sizes are checked in the handler, not by Field limits: a validation error echoes the input back,
    # and the input here is the password and the whole file.
    filename: str = Field(max_length=200)
    password: str | None = None
    content_base64: str


class PreviewRow(BaseModel):
    index: int  # position among the parsed rows
    page: int
    date: str
    time: str | None
    narration: str
    reference: str | None
    direction: Literal["debit", "credit"]
    amount_paise: int
    payment: bool  # a card bill payment received


class MatchRow(PreviewRow):
    transaction_id: str
    current_amount_paise: int  # what the transaction shows now, through the user's edits
    current_date: str
    payee: str | None
    edited: list[str]  # fields the user edited; the statement never overwrites them


class StatementPreview(BaseModel):
    id: str
    filename: str
    template_name: str | None
    created_at: str
    expires_at: str
    account_id: str | None
    account_name: str | None
    account_kind: Literal["bank", "credit_card"]
    new: list[PreviewRow]
    matches: list[MatchRow]
    unparseable: list[dict[str, Any]]  # {page, raw_cells, reason}, as the diagnostic records them
    diagnostic: dict[str, Any]  # the full §4.8 record, for the details toggle


class StagedSummary(BaseModel):
    id: str
    filename: str
    account_name: str | None
    outcome: str
    created_at: str
    expires_at: str


class Rebind(BaseModel):
    account_id: str


class MergeRequest(BaseModel):
    accept_gap: bool = False  # merge although the rows don't add up (the gap is kept on record)


def _found(session: Session, user_id: str, staging_id: str) -> StatementStaging:
    staging: StatementStaging | None = load(session, user_id, staging_id)
    if staging is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This preview has expired or was discarded.")
    return staging


def _preview(session: Session, user_id: str, staging: StatementStaging) -> StatementPreview:
    rows: list[StatementRow] = rows_of(staging)
    dispositions: list[str] = json.loads(staging.row_dispositions)
    matched: dict[int, str] = {i: d.removeprefix("matches:") for i, d in enumerate(dispositions) if d != "new"}
    current: dict[str, ListedTransaction] = list_by_ids(session, user_id, list(matched.values()))
    new: list[PreviewRow] = []
    matches: list[MatchRow] = []
    for i, r in enumerate(rows):
        base: dict[str, Any] = {"index": i, "page": r.page, "date": r.date, "time": r.time, "narration": r.narration,
                                "reference": r.reference, "direction": r.direction, "amount_paise": r.amount,
                                "payment": r.payment}
        listed: ListedTransaction | None = current.get(matched.get(i, ""))
        if listed is None:  # new, or its match has since merged away: the merge decides afresh
            new.append(PreviewRow(**base))
            continue
        matches.append(MatchRow(**base, transaction_id=listed.transaction.id,
                                current_amount_paise=listed.transaction.amount,
                                current_date=listed.transaction.transaction_date,
                                payee=listed.payee_name or listed.transaction.payee_raw,
                                edited=sorted({e.field for e in listed.edits})))
    diagnostic: dict[str, Any] = json.loads(staging.validation_result)
    account: Account | None = session.get(Account, staging.account_id) if staging.account_id else None
    template: ParserTemplate | None = session.get(ParserTemplate, staging.template_id) if staging.template_id else None
    kind: str = account_kind(staging)
    assert kind in ("bank", "credit_card")
    return StatementPreview(
        id=staging.id, filename=staging.filename, template_name=template.name if template else None,
        created_at=staging.created_at, expires_at=staging.expires_at,
        account_id=account.id if account else None, account_name=account.display_name if account else None,
        account_kind="bank" if kind == "bank" else "credit_card", new=new, matches=matches,
        unparseable=diagnostic.get("unparseable_rows", []), diagnostic=diagnostic)


@router.post("", response_model=StatementPreview, status_code=status.HTTP_201_CREATED)
def upload(body: Upload, user: CurrentUser = Depends(current_user),
           db: Database = Depends(get_db)) -> StatementPreview:
    if len(body.content_base64) > (MAX_PDF_BYTES * 4) // 3 + 4:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That file is over 10 MB.")
    if body.password is not None and len(body.password) > MAX_PASSWORD:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That password is too long to be a statement's.")
    try:
        data: bytes = base64.b64decode(body.content_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That file couldn't be read.") from None
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That file is over 10 MB.")
    try:
        with db.read_session() as s:
            found: ReadStatement = read_statement(s, user.id, data, body.password)
    except StatementError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from None
    with db.write_session() as s:
        staging: StatementStaging = stage(s, user.id, body.filename, hashlib.sha256(data).hexdigest(), found)
        return _preview(s, user.id, staging)


@router.get("", response_model=list[StagedSummary])
def open_previews(user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)) -> list[StagedSummary]:
    with db.read_session() as s:
        rows: list[tuple[StatementStaging, str | None]] = [(r[0], r[1]) for r in s.execute(
            select(StatementStaging, Account.display_name).outerjoin(Account, Account.id == StatementStaging.account_id)
            .where(StatementStaging.user_id == user.id, StatementStaging.expires_at > utc_now())
            .order_by(StatementStaging.created_at.desc()))]
        return [StagedSummary(id=r.id, filename=r.filename, account_name=name,
                              outcome=json.loads(r.validation_result)["outcome"], created_at=r.created_at,
                              expires_at=r.expires_at) for r, name in rows]


@router.get("/{staging_id}", response_model=StatementPreview)
def preview(staging_id: str, user: CurrentUser = Depends(current_user),
            db: Database = Depends(get_db)) -> StatementPreview:
    with db.read_session() as s:
        return _preview(s, user.id, _found(s, user.id, staging_id))


@router.patch("/{staging_id}", response_model=StatementPreview)
def pick_account(staging_id: str, body: Rebind, user: CurrentUser = Depends(current_user),
                 db: Database = Depends(get_db)) -> StatementPreview:
    with db.write_session() as s:
        _found(s, user.id, staging_id)
        try:
            staging: StatementStaging = rebind(s, user.id, staging_id, body.account_id)
        except StatementError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from None
        return _preview(s, user.id, staging)


@router.delete("/{staging_id}", status_code=status.HTTP_204_NO_CONTENT)
def discard(staging_id: str, user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)) -> Response:
    with db.write_session() as s:
        s.delete(_found(s, user.id, staging_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{staging_id}/merge", response_model=JobRef, status_code=status.HTTP_202_ACCEPTED)
def merge(staging_id: str, body: MergeRequest, request: Request, user: CurrentUser = Depends(current_user),
          db: Database = Depends(get_db)) -> JobRef:
    """Checked here so the person gets the sentence at once; the job checks again, as the authority."""
    with db.read_session() as s:
        staging: StatementStaging = _found(s, user.id, staging_id)
        account: Account | None = s.get(Account, staging.account_id) if staging.account_id else None
        if account is None or account.archived_at is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Pick an active account for this statement first.")
        if json.loads(staging.validation_result)["outcome"] == "failed" and not body.accept_gap:
            raise HTTPException(status.HTTP_409_CONFLICT, "The rows don't add up to the statement's closing "
                                "balance. Merge anyway to keep the gap on record, or discard the statement.")
    jobs: JobQueue = request.app.state.jobs
    return JobRef(job_id=jobs.enqueue("statement_merge", user.id,
                                      {"staging_id": staging_id, "accept_gap": body.accept_gap}))
