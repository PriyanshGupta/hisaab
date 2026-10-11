"""/api/categories (per user) and /api/payees (global), plus payee rules.

A payee's category is per user: GET /api/payees/{id} reports the caller's own
rule, and PUT /api/payees/{id}/category sets or clears it (category_id null).
Errors come from the repositories, mapped in main._register_error_handlers.
"""

from typing import Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel

from hisaab.api.deps import CurrentUser, current_user, get_db
from hisaab.categories import repository as cats
from hisaab.db import Database
from hisaab.models import Category, Payee
from hisaab.payees import repository as payees

categories_router = APIRouter(prefix="/api/categories", tags=["categories"])
payees_router = APIRouter(prefix="/api/payees", tags=["payees"])


class CategoryOut(BaseModel):
    id: str
    name: str
    archived_at: str | None

    @classmethod
    def of(cls, c: Category) -> "CategoryOut":
        return cls(id=c.id, name=c.name, archived_at=c.archived_at)


class CategoryName(BaseModel):
    name: str


class PayeeOut(BaseModel):
    id: str
    name: str
    type: Literal["merchant", "person"]
    category_id: str | None = None  # the caller's rule for this payee (only on GET /{id} and PUT)

    @classmethod
    def of(cls, p: Payee, category_id: str | None = None) -> "PayeeOut":
        return cls(id=p.id, name=p.normalized_name, type=p.type, category_id=category_id)


class NewPayee(BaseModel):
    name: str
    type: Literal["merchant", "person"] = "merchant"


class PayeeChange(BaseModel):
    name: str | None = None
    type: Literal["merchant", "person"] | None = None


class PayeeRule(BaseModel):
    category_id: str | None


# --- categories --------------------------------------------------------------------

@categories_router.get("", response_model=list[CategoryOut])
def list_categories(user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)):
    with db.read_session() as s:
        return [CategoryOut.of(c) for c in cats.list_categories(s, user.id)]


@categories_router.post("", response_model=CategoryOut, status_code=status.HTTP_201_CREATED)
def create_category(body: CategoryName, request: Request, user: CurrentUser = Depends(current_user),
                    db: Database = Depends(get_db)):
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        return CategoryOut.of(cats.create_category(s, user.id, body.name))


@categories_router.patch("/{category_id}", response_model=CategoryOut)
def rename_category(category_id: str, body: CategoryName, request: Request,
                    user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)):
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        return CategoryOut.of(cats.rename_category(s, user.id, category_id, body.name))


@categories_router.post("/{category_id}/archive", response_model=CategoryOut)
def archive_category(category_id: str, request: Request, user: CurrentUser = Depends(current_user),
                     db: Database = Depends(get_db)):
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        return CategoryOut.of(cats.archive_category(s, user.id, category_id))


@categories_router.post("/{category_id}/restore", response_model=CategoryOut)
def restore_category(category_id: str, request: Request, user: CurrentUser = Depends(current_user),
                     db: Database = Depends(get_db)):
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        return CategoryOut.of(cats.restore_category(s, user.id, category_id))


# --- payees ------------------------------------------------------------------------

@payees_router.get("", response_model=list[PayeeOut])
def list_payees(q: str | None = None, _user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)):
    with db.read_session() as s:
        return [PayeeOut.of(p) for p in payees.list_payees(s, q)]


@payees_router.post("", response_model=PayeeOut, status_code=status.HTTP_201_CREATED)
def create_payee(body: NewPayee, request: Request, _user: CurrentUser = Depends(current_user),
                 db: Database = Depends(get_db)):
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        return PayeeOut.of(payees.create_payee(s, body.name, body.type))


@payees_router.get("/{payee_id}", response_model=PayeeOut)
def get_payee(payee_id: str, user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)):
    with db.read_session() as s:
        return PayeeOut.of(payees.get_payee(s, payee_id), cats.payee_rule(s, user.id, payee_id))


@payees_router.patch("/{payee_id}", response_model=PayeeOut)
def update_payee(payee_id: str, body: PayeeChange, request: Request, user: CurrentUser = Depends(current_user),
                 db: Database = Depends(get_db)):
    """Renaming a payee renames it for every user (payees are global)."""
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        payee = payees.update_payee(s, payee_id, name=body.name, type=body.type)
        return PayeeOut.of(payee, cats.payee_rule(s, user.id, payee_id))


@payees_router.put("/{payee_id}/category", response_model=PayeeOut)
def set_payee_category(payee_id: str, body: PayeeRule, request: Request, user: CurrentUser = Depends(current_user),
                       db: Database = Depends(get_db)):
    """File all of this payee's transactions under a category, for the caller only."""
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        cats.set_payee_category(s, user.id, payee_id, body.category_id)
        return PayeeOut.of(payees.get_payee(s, payee_id), body.category_id)
