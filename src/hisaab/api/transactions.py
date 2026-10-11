"""/api/transactions: manual entry, the transaction list, corrections, undo.

Amounts cross the API as integer paise (StrictInt, *_paise names). Corrections
are a PATCH of the fields to change, applied and validated together; the
response shows each correction with the source's value beside it
(edits[].prior_value), which is what "undo" returns to. Errors come from the
repository and are mapped in main._register_error_handlers.
"""

from typing import Any, Literal, TypeAlias, cast

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, StrictInt

from hisaab.api.deps import CurrentUser, current_user, get_db
from hisaab.db import Database
from hisaab.models import Transaction, TransactionEffective
from hisaab.transactions import repository as txns

router: APIRouter = APIRouter(prefix="/api/transactions", tags=["transactions"])

EditableField: TypeAlias = Literal["amount", "kind", "transaction_date", "from_account_id", "to_account_id",
                        "payee_id", "category_override_id"]
CategorySource: TypeAlias = Literal["override", "rule"]
Provenance: TypeAlias = Literal["email", "statement", "manual"]


class EditOut(BaseModel):
    field: str
    prior_value: str | None  # what the source said
    new_value: str | None


class TransactionOut(BaseModel):
    id: str
    kind: str
    amount_paise: int
    transaction_date: str
    transaction_time: str | None
    from_account_id: str | None
    to_account_id: str | None
    payee_raw: str | None
    payee_id: str | None
    payee_name: str | None
    category_id: str | None  # resolved at read time; None = uncategorized
    category_source: CategorySource | None  # which mechanism applies
    status: str
    provenance: Provenance
    edits: list[EditOut]


class NewTransaction(BaseModel):
    kind: Literal["expense", "income", "transfer", "refund"]
    amount_paise: StrictInt
    transaction_date: str
    transaction_time: str | None = None
    from_account_id: str | None = None
    to_account_id: str | None = None
    payee: str | None = None
    category_id: str | None = None


class TransactionChange(BaseModel):
    """Only the fields present are changed; null on an account field makes that leg external."""
    amount_paise: StrictInt | None = None
    kind: str | None = None
    transaction_date: str | None = None
    from_account_id: str | None = None
    to_account_id: str | None = None
    payee_id: str | None = None
    category_override_id: str | None = None  # null clears the override (the payee rule applies again)


class RevertRequest(BaseModel):
    fields: list[EditableField]


def _out(row: txns.ListedTransaction) -> TransactionOut:
    t: TransactionEffective = row.transaction
    return TransactionOut(
        id=t.id, kind=t.kind, amount_paise=t.amount, transaction_date=t.transaction_date,
        transaction_time=t.transaction_time, from_account_id=t.from_account_id, to_account_id=t.to_account_id,
        payee_raw=t.payee_raw, payee_id=t.payee_id, payee_name=row.payee_name, category_id=row.category_id,
        # the resolution query and sources.kind's CHECK constraint hold these to their literals
        category_source=cast(CategorySource | None, row.category_source), status=t.status,
        provenance=cast(Provenance, row.provenance),
        edits=[EditOut(field=e.field, prior_value=e.prior_value, new_value=e.new_value) for e in row.edits],
    )


@router.get("", response_model=list[TransactionOut])
def list_transactions(limit: int = 200, user: CurrentUser = Depends(current_user),
                      db: Database = Depends(get_db)) -> list[TransactionOut]:
    with db.read_session() as s:
        return [_out(r) for r in txns.list_transactions(s, user.id, limit=min(max(limit, 1), 1000))]


@router.post("", response_model=TransactionOut, status_code=status.HTTP_201_CREATED)
def create_transaction(body: NewTransaction, request: Request, user: CurrentUser = Depends(current_user),
                       db: Database = Depends(get_db)) -> TransactionOut:
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        txn: Transaction = txns.create_manual(
            s, user.id, kind=body.kind, amount=body.amount_paise, transaction_date=body.transaction_date,
            transaction_time=body.transaction_time, from_account_id=body.from_account_id,
            to_account_id=body.to_account_id, payee_raw=body.payee, category_id=body.category_id,
        )
        return _out(txns.get_listed(s, user.id, txn.id))


@router.get("/{txn_id}", response_model=TransactionOut)
def get_transaction(txn_id: str, user: CurrentUser = Depends(current_user),
                    db: Database = Depends(get_db)) -> TransactionOut:
    with db.read_session() as s:
        return _out(txns.get_listed(s, user.id, txn_id))


@router.patch("/{txn_id}", response_model=TransactionOut)
def correct_transaction(txn_id: str, body: TransactionChange, request: Request,
                        user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)) -> TransactionOut:
    changes: dict[str, Any] = body.model_dump(exclude_unset=True)
    if "amount_paise" in changes:
        changes["amount"] = changes.pop("amount_paise")
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        txns.apply_edits(s, user.id, txn_id, changes)
        return _out(txns.get_listed(s, user.id, txn_id))


@router.post("/{txn_id}/revert", response_model=TransactionOut)
def undo_corrections(txn_id: str, body: RevertRequest, request: Request,
                     user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)) -> TransactionOut:
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        txns.revert(s, user.id, txn_id, list(body.fields))
        return _out(txns.get_listed(s, user.id, txn_id))


@router.delete("/{txn_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_transaction(txn_id: str, request: Request, confirm: bool = False,
                       user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)) -> Response:
    """Manual entries only. With corrections on it, needs ?confirm=true (they'd be lost)."""
    with db.write_session(timeout=request.app.state.writer_timeout) as s:
        txns.delete_manual(s, user.id, txn_id, confirm_edits_lost=confirm)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
