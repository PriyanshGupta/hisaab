"""/api/balances and manual checkpoints (tech-design §8.3, §8.4; rulings 2026-10-20).

    GET  /api/balances                              every active account: the verified figure, movements since,
                                                    and its gaps (never summed across accounts)
    POST /api/accounts/{id}/checkpoints             a manual balance; checked at once
    POST /api/balances/checks/{observation_id}/accept   write the adjustment for that gap
    POST /api/balances/checks/{observation_id}/close    close as investigated, once the gap is gone

A gap is listed while the live gap exceeds the tolerance (state "open"), or while an open row's live gap is
back within it (state "fixed": the person closes it). Gaps are computed from today's data on every read.
"""

from datetime import date
from typing import Literal, TypeAlias, cast

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, StrictInt
from sqlalchemy import select
from sqlalchemy.orm import Session

from hisaab.accounts.repository import list_accounts
from hisaab.api.deps import CurrentUser, current_user, get_db
from hisaab.balances.checks import CheckError, NoSuch, accept, add_checkpoint, close, tolerance
from hisaab.balances.ledger import Anchor, Figure, checks, figure
from hisaab.db import Database
from hisaab.models import Account, BalanceDelta

router: APIRouter = APIRouter(tags=["balances"])

ObservationSource: TypeAlias = Literal["opening", "statement", "email", "manual"]


class Verified(BaseModel):
    balance_paise: int
    on: str
    source: ObservationSource


class Gap(BaseModel):
    observation_id: str
    observed_on: str
    observed_paise: int
    expected_paise: int
    gap_paise: int  # the bank's figure minus Hisaab's: on a card, positive means more is owed
    since: str  # the anchor the interval starts from (arose_from)
    since_source: ObservationSource
    observed_source: ObservationSource  # who produced the observed figure: the statement, an email, or the person
    state: Literal["open", "fixed"]


class AccountBalance(BaseModel):
    account_id: str
    name: str
    type: Literal["bank", "credit_card", "cash"]
    verified: Verified | None  # None: no opening balance and no observation yet
    balance_paise: int | None  # verified + movements since
    transactions_since: int
    gaps: list[Gap]  # newest first


class Checkpoint(BaseModel):
    balance_paise: StrictInt
    on: date


def _source(anchor: Anchor) -> ObservationSource:
    return cast(ObservationSource, anchor.source)


def _balance(session: Session, user_id: str, account: Account) -> AccountBalance:
    limit: int = tolerance(session, user_id)
    open_rows: set[str] = set(session.scalars(select(BalanceDelta.observation_id).where(
        BalanceDelta.user_id == user_id, BalanceDelta.account_id == account.id, BalanceDelta.status == "open")))
    gaps: list[Gap] = []
    for c in checks(session, user_id, account):
        state: Literal["open", "fixed"] | None = (
            "open" if abs(c.gap) > limit else "fixed" if c.observation_id in open_rows else None)
        if state is not None:
            gaps.append(Gap(observation_id=c.observation_id, observed_on=c.observed_on, observed_paise=c.observed,
                            expected_paise=c.expected, gap_paise=c.gap, since=c.since.on,
                            since_source=_source(c.since), observed_source=cast(ObservationSource, c.observed_source),
                            state=state))
    fig: Figure = figure(session, user_id, account)
    verified: Verified | None = (Verified(balance_paise=fig.anchor.balance, on=fig.anchor.on,
                                          source=_source(fig.anchor)) if fig.anchor else None)
    return AccountBalance(account_id=account.id, name=account.display_name,
                          type=cast(Literal["bank", "credit_card", "cash"], account.type), verified=verified,
                          balance_paise=fig.balance, transactions_since=fig.transactions_since,
                          gaps=list(reversed(gaps)))


def _refused(e: CheckError) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND if isinstance(e, NoSuch) else
                         status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))


@router.get("/api/balances", response_model=list[AccountBalance])
def get_balances(user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)) -> list[AccountBalance]:
    with db.read_session() as s:
        return [_balance(s, user.id, a) for a in list_accounts(s, user.id)]


@router.post("/api/accounts/{account_id}/checkpoints", response_model=AccountBalance,
             status_code=status.HTTP_201_CREATED)
def post_checkpoint(account_id: str, body: Checkpoint, user: CurrentUser = Depends(current_user),
                    db: Database = Depends(get_db)) -> AccountBalance:
    with db.write_session() as s:
        try:
            add_checkpoint(s, user.id, account_id, body.balance_paise, body.on.isoformat())
        except CheckError as e:
            raise _refused(e) from None
        return _balance(s, user.id, s.get_one(Account, account_id))


def _resolve(observation_id: str, user_id: str, db: Database, action: str) -> AccountBalance:
    with db.write_session() as s:
        try:
            row: BalanceDelta = (accept if action == "accept" else close)(s, user_id, observation_id)
        except CheckError as e:
            raise _refused(e) from None
        return _balance(s, user_id, s.get_one(Account, row.account_id))


@router.post("/api/balances/checks/{observation_id}/accept", response_model=AccountBalance)
def post_accept(observation_id: str, user: CurrentUser = Depends(current_user),
                db: Database = Depends(get_db)) -> AccountBalance:
    return _resolve(observation_id, user.id, db, "accept")


@router.post("/api/balances/checks/{observation_id}/close", response_model=AccountBalance)
def post_close(observation_id: str, user: CurrentUser = Depends(current_user),
               db: Database = Depends(get_db)) -> AccountBalance:
    return _resolve(observation_id, user.id, db, "close")
