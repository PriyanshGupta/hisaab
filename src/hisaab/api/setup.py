"""/api/setup: first-run setup in its strict order (tech-design §5.4; PRD §6.1).

    accounts -> opening balances -> Gmail -> backfill -> bulk categorization

The step is worked out from what exists, never stored:
1. clock set (the first sync, i.e. the backfill, finished): "categorize" while
   the user has uncategorized transactions and none categorized yet, else
   "done". A backfill that found nothing lands on the report, not an empty screen.
2. no active account: "accounts".
3. Gmail not connected: "gmail" (the screen shows the balances check first).
4. otherwise: "backfill", with the latest backfill job and its progress.

The order is also enforced where it matters: connecting Gmail during first
run needs an account, and every opening balance dated on or before the
backfill start (PRD §4.11), or the review queue opens with hundreds of
unresolved accounts and the balances are wrong from day one.

/api/setup/uncategorized feeds the bulk categorization screen: uncategorized
transactions grouped by recognised payee, the rest counted.
"""

from datetime import date
from typing import Literal, TypeAlias

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Subquery, func, select
from sqlalchemy.orm import Session

from hisaab.api.deps import CurrentUser, current_user, get_db
from hisaab.api.jobs import JobOut, job_out
from hisaab.categories.resolution import categorized
from hisaab.db import Database
from hisaab.ingest.sync import backfill_start
from hisaab.models import Account, GmailConnection, Job

router: APIRouter = APIRouter(prefix="/api/setup", tags=["setup"])

SetupStep: TypeAlias = Literal["accounts", "gmail", "backfill", "categorize", "done"]
CATEGORIZABLE: tuple[str, ...] = ("expense", "income", "refund")  # transfers and adjustments never take one


class SetupOut(BaseModel):
    step: SetupStep
    backfill_start: str  # the default opening-balance date
    job: JobOut | None  # the latest backfill job, with progress


class PayeeGroup(BaseModel):
    payee_id: str
    payee_name: str
    count: int


class UncategorizedOut(BaseModel):
    groups: list[PayeeGroup]  # most transactions first
    unnamed: int  # uncategorized, with no recognised payee


def _day(d: date) -> str:
    return f"{d.day} {d:%b %Y}"


def first_run_problem(session: Session, user_id: str) -> str | None:
    """Why Gmail can't be connected yet during first run, or None (also None once the first sync is done)."""
    row: GmailConnection | None = session.get(GmailConnection, user_id)
    if row is not None and row.last_successful_sync_at is not None:
        return None
    accounts: list[Account] = list(session.scalars(select(Account).where(
        Account.user_id == user_id, Account.archived_at.is_(None)).order_by(Account.display_name)))
    if not accounts:
        return "Add your accounts first."
    start: date = backfill_start(session, user_id)
    late: list[Account] = [a for a in accounts if date.fromisoformat(a.opening_balance_date) > start]
    if not late:
        return None
    named: str = "; ".join(f"{a.display_name} is dated {_day(date.fromisoformat(a.opening_balance_date))}" for a in late)
    return (f"Opening balances must be on or before {_day(start)}, where your history starts: {named}. "
            "If an account is newer than that, enter 0 on that date.")


def _uncategorized(user_id: str) -> Subquery:
    return categorized(user_id).subquery()


def _step(session: Session, user_id: str) -> SetupStep:
    row: GmailConnection | None = session.get(GmailConnection, user_id)
    if row is not None and row.last_successful_sync_at is not None:
        q: Subquery = _uncategorized(user_id)
        open_, filed = session.execute(select(
            func.count().filter(q.c.category_id.is_(None)), func.count().filter(q.c.category_id.is_not(None)),
        ).where(q.c.kind.in_(CATEGORIZABLE))).one()
        return "categorize" if open_ and not filed else "done"
    if session.scalar(select(func.count()).select_from(Account).where(
            Account.user_id == user_id, Account.archived_at.is_(None))) == 0:
        return "accounts"
    return "backfill" if row is not None and row.status == "connected" else "gmail"


@router.get("", response_model=SetupOut)
def setup_state(user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)) -> SetupOut:
    with db.read_session() as s:
        job: Job | None = s.scalars(select(Job).where(Job.user_id == user.id, Job.kind == "backfill")
                                    .order_by(Job.created_at.desc(), Job.id.desc()).limit(1)).first()
        return SetupOut(step=_step(s, user.id), backfill_start=backfill_start(s, user.id).isoformat(),
                        job=job_out(job) if job else None)


@router.get("/uncategorized", response_model=UncategorizedOut)
def uncategorized(user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)) -> UncategorizedOut:
    q: Subquery = _uncategorized(user.id)
    with db.read_session() as s:
        rows: list[tuple[str | None, str | None, int]] = list(s.execute(
            select(q.c.payee_id, q.c.payee_name, func.count())
            .where(q.c.category_id.is_(None), q.c.kind.in_(CATEGORIZABLE))
            .group_by(q.c.payee_id, q.c.payee_name).order_by(func.count().desc(), q.c.payee_name)).tuples())
    return UncategorizedOut(
        groups=[PayeeGroup(payee_id=p, payee_name=n or "", count=c) for p, n, c in rows if p is not None],
        unnamed=sum(c for p, _, c in rows if p is None))
