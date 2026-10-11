"""/api/accounts: a user's own accounts and their identifiers (PRD §3.3, §6.6).

Money crosses the API as integer paise in fields named *_paise, so the unit is
in the name and floats are rejected outright (StrictInt). For a credit card,
opening_balance_paise is the amount OWED, positive (PRD §10.3).

Errors: repository exceptions propagate and main._register_error_handlers turns
them into responses: a rule violation is 422 with a sentence for the person, a
clash with another active account (same last4 / identifier) is 409 naming that
account, and anything not belonging to the caller is 404.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, StrictInt

from hisaab.accounts import repository as accounts
from hisaab.api.deps import CurrentUser, current_user, get_db
from hisaab.db import Database
from hisaab.models import Account

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

AccountType = Literal["bank", "credit_card", "cash"]


class IdentifierOut(BaseModel):
    id: str
    kind: str
    identifier: str
    removable: bool  # false for the last 4 digits the account was created with


class AccountOut(BaseModel):
    id: str
    type: AccountType
    display_name: str
    institution: str | None
    last4: str | None
    opening_balance_paise: int
    opening_balance_date: str
    archived_at: str | None
    created_at: str
    identifiers: list[IdentifierOut]


class NewAccount(BaseModel):
    type: AccountType
    display_name: str
    institution: str | None = None
    last4: str | None = None
    opening_balance_paise: StrictInt
    opening_balance_date: str


class AccountChange(BaseModel):
    display_name: str | None = None
    institution: str | None = None
    opening_balance_paise: StrictInt | None = None
    opening_balance_date: str | None = None


class NewIdentifier(BaseModel):
    kind: Literal["vpa", "other"]
    identifier: str


def _out(session, user_id: str, account: Account) -> AccountOut:
    return AccountOut(
        id=account.id, type=account.type, display_name=account.display_name, institution=account.institution,
        last4=account.last4, opening_balance_paise=account.opening_balance,
        opening_balance_date=account.opening_balance_date, archived_at=account.archived_at,
        created_at=account.created_at,
        identifiers=[
            IdentifierOut(id=i.id, kind=i.kind, identifier=i.identifier,
                          removable=i.kind in accounts.USER_IDENTIFIER_KINDS)
            for i in accounts.active_identifiers(session, user_id, account.id)
        ],
    )


@router.get("", response_model=list[AccountOut])
def list_accounts(include_archived: bool = False, user: CurrentUser = Depends(current_user),
                  db: Database = Depends(get_db)):
    with db.read_session() as s:
        return [_out(s, user.id, a) for a in accounts.list_accounts(s, user.id, include_archived)]


@router.post("", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
def create_account(body: NewAccount, request: Request, user: CurrentUser = Depends(current_user),
                   db: Database = Depends(get_db)):
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        account = accounts.create_account(
            s, user.id, type=body.type, display_name=body.display_name, institution=body.institution,
            last4=body.last4, opening_balance=body.opening_balance_paise,
            opening_balance_date=body.opening_balance_date,
        )
        return _out(s, user.id, account)


# Declared before /{account_id} so "resolve" is not taken for an id.
@router.get("/resolve", response_model=AccountOut)
def resolve_identifier(identifier: str = Query(min_length=1), user: CurrentUser = Depends(current_user),
                       db: Database = Depends(get_db)):
    """Which of my active accounts does this raw identifier belong to? 404 if none, or if ambiguous."""
    with db.read_session() as s:
        account = accounts.resolve_identifier(s, user.id, identifier)
        if account is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No active account has that identifier")
        return _out(s, user.id, account)


@router.get("/{account_id}", response_model=AccountOut)
def get_account(account_id: str, user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)):
    with db.read_session() as s:
        return _out(s, user.id, accounts.get_account(s, user.id, account_id))


@router.patch("/{account_id}", response_model=AccountOut)
def update_account(account_id: str, body: AccountChange, request: Request,
                   user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)):
    changes = body.model_dump(exclude_unset=True)
    if "opening_balance_paise" in changes:
        changes["opening_balance"] = changes.pop("opening_balance_paise")
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        return _out(s, user.id, accounts.update_account(s, user.id, account_id, **changes))


@router.post("/{account_id}/archive", response_model=AccountOut)
def archive_account(account_id: str, request: Request, user: CurrentUser = Depends(current_user),
                    db: Database = Depends(get_db)):
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        return _out(s, user.id, accounts.archive_account(s, user.id, account_id))


@router.post("/{account_id}/identifiers", response_model=IdentifierOut, status_code=status.HTTP_201_CREATED)
def add_identifier(account_id: str, body: NewIdentifier, request: Request,
                   user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)):
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        i = accounts.add_identifier(s, user.id, account_id, body.kind, body.identifier)
        return IdentifierOut(id=i.id, kind=i.kind, identifier=i.identifier, removable=True)


@router.delete("/{account_id}/identifiers/{identifier_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_identifier(account_id: str, identifier_id: str, request: Request,
                      user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)):
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        accounts.remove_identifier(s, user.id, account_id, identifier_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
