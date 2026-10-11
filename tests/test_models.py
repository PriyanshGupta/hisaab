import re
import time
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from hisaab.models import (
    Account,
    ReadOnlyViewError,
    Source,
    Transaction,
    TransactionEdit,
    TransactionEffective,
    User,
    new_id,
    utc_now,
)


def test_utc_now_is_fixed_width_iso_with_z() -> None:
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", utc_now())


def test_utc_now_sorts_lexicographically_in_time_order() -> None:
    earlier: str = utc_now()
    time.sleep(0.002)
    assert utc_now() > earlier


def test_new_id_is_a_uuid4_string() -> None:
    assert uuid.UUID(new_id()).version == 4


@pytest.fixture
def session(migrated_engine: Engine) -> Iterator[Session]:
    with Session(migrated_engine) as s:
        yield s


def _seed(session: Session) -> tuple[User, Transaction]:
    user: User = User(username="asha", password_hash="x", display_name="Asha", role="admin")
    session.add(user)
    session.flush()
    account: Account = Account(user_id=user.id, type="bank", display_name="Savings", opening_balance=0,
                      opening_balance_date="2026-07-01")
    now: str = utc_now()
    source: Source = Source(ingest_seq=1, user_id=user.id, kind="manual", content_hash="h",
                    received_at=now, ingested_at=now, processing_state="parsed")
    session.add_all([account, source])
    session.flush()
    txn: Transaction = Transaction(user_id=user.id, derived_key="k1", amount=12345, kind="expense",
                      from_account_id=account.id, transaction_date="2026-08-20", status="confirmed",
                      primary_source_id=source.id)
    session.add(txn)
    session.commit()
    return user, txn


def test_orm_defaults_fill_ids_and_timestamps(session: Session) -> None:
    user, txn = _seed(session)
    assert uuid.UUID(user.id) and uuid.UUID(txn.id)
    assert user.created_at.endswith("Z") and txn.updated_at.endswith("Z")


def test_effective_model_reads_overlaid_values(session: Session) -> None:
    user, txn = _seed(session)
    session.add(TransactionEdit(transaction_id=txn.id, user_id=user.id, field="amount",
                                prior_value="12345", new_value="50000"))
    session.commit()
    row: TransactionEffective = session.scalars(select(TransactionEffective).where(TransactionEffective.id == txn.id)).one()
    assert (row.amount, row.derived_amount, row.user_id) == (50000, 12345, user.id)


def test_effective_model_refuses_new_rows(session: Session) -> None:
    session.add(TransactionEffective(id=new_id(), amount=1))
    with pytest.raises(ReadOnlyViewError):
        session.flush()


def test_effective_model_refuses_updates(session: Session) -> None:
    _, txn = _seed(session)
    row: TransactionEffective = session.get_one(TransactionEffective, txn.id)
    row.amount = 1
    with pytest.raises(ReadOnlyViewError):
        session.flush()


def test_effective_model_refuses_deletes(session: Session) -> None:
    _, txn = _seed(session)
    session.delete(session.get(TransactionEffective, txn.id))
    with pytest.raises(ReadOnlyViewError):
        session.flush()
