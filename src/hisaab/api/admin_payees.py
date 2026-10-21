"""/api/admin/payee-names: captured payee corrections (PRD §5.1 Loop 1, §5.6). Admin only.

GET lists each correction once: the raw text, the name chosen, how many transactions across every user share
that text, and who reported it. POST /promote writes a global pattern (exact by default, or as the admin
edited it) and applies it to history in the database alone, with no Gmail fetch and no job: payee_raw is kept on
every transaction for exactly this. POST /dismiss sets a correction aside. Members get 403, anonymous callers
401, before any handler runs (require_admin).
"""

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel

from hisaab.api.deps import CurrentUser, get_db, require_admin
from hisaab.db import Database
from hisaab.payees import corrections

router: APIRouter = APIRouter(prefix="/api/admin/payee-names", tags=["admin"], dependencies=[Depends(require_admin)])


class Reporter(BaseModel):
    user: str
    count: int


class CorrectionOut(BaseModel):
    key: str
    raw: str
    payee_id: str
    payee_name: str
    shared: int
    reporters: list[Reporter]


class PromoteIn(BaseModel):
    match_type: str = "exact"
    pattern: str
    payee_id: str | None = None  # an existing payee, or
    payee_name: str | None = None  # a name: found, or created


class PromoteOut(BaseModel):
    pattern_id: str
    named: int
    users: int


class DismissIn(BaseModel):
    key: str
    payee_id: str


@router.get("")
def payee_names(db: Database = Depends(get_db)) -> list[CorrectionOut]:
    with db.read_session() as s:
        return [CorrectionOut(key=c.key, raw=c.raw, payee_id=c.payee_id, payee_name=c.payee_name, shared=c.shared,
                              reporters=[Reporter(user=u, count=n) for u, n in c.reporters])
                for c in corrections.corrections(s)]


@router.post("/promote")
def promote(body: PromoteIn, request: Request, db: Database = Depends(get_db)) -> PromoteOut:
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        result: corrections.PromoteResult = corrections.promote(
            s, payee_id=body.payee_id, payee_name=body.payee_name, match_type=body.match_type, pattern=body.pattern)
    return PromoteOut(pattern_id=result.pattern_id, named=result.named, users=result.users)


@router.post("/dismiss", status_code=status.HTTP_204_NO_CONTENT)
def dismiss(body: DismissIn, request: Request, user: CurrentUser = Depends(require_admin),
            db: Database = Depends(get_db)) -> Response:
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        corrections.dismiss(s, body.key, body.payee_id, user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
