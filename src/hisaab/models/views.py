"""Read-only mapping onto the transactions_effective view (tech-design §3.5, amended).

The view overlays transaction_edits onto transactions: for each overlaid field,
if an edit row exists its value wins, even when that value is NULL ("edited to
null"); otherwise the parser's value shows. Every read path goes through it, so
reports can aggregate effective values in SQL.

The view's DDL lives in migration 0001 (migrations are frozen; the SQL is not
importable app code). This module only describes its columns. The table object
sits in its own MetaData so autogenerate never tries to CREATE TABLE it.

Writes are impossible in SQLite anyway (a view without INSTEAD OF triggers),
but the flush guard below fails earlier and with a clear message.
"""

from sqlalchemy import BigInteger, Column, MetaData, Table, Text, event
from sqlalchemy.orm import Session

from hisaab.models.base import Base

view_metadata = MetaData()

transactions_effective = Table(
    "transactions_effective",
    view_metadata,
    Column("id", Text, primary_key=True),
    Column("user_id", Text),
    Column("derived_key", Text),
    # Overlaid fields: edit value if an edit row exists, else the parser's value.
    Column("amount", BigInteger),
    Column("kind", Text),
    Column("payee_id", Text),
    Column("transaction_date", Text),
    Column("from_account_id", Text),
    Column("to_account_id", Text),
    # NULL once the user has assigned that leg's account by edit.
    Column("unresolved_from_identifier", Text),
    Column("unresolved_to_identifier", Text),
    # Edit-only fields (no parser counterpart).
    Column("category_override_id", Text),
    Column("review_acknowledged", Text),  # the acknowledged amount, as text
    # Pass-through base columns.
    Column("status", Text),
    Column("review_reason", Text),
    Column("payee_raw", Text),
    Column("primary_source_id", Text),
    Column("reference_number", Text),
    Column("transaction_time", Text),
    Column("posted_date", Text),
    Column("reverses_transaction_id", Text),
    Column("refunds_transaction_id", Text),
    # The parser's values, for conflict review and undo.
    Column("derived_kind", Text),
    Column("derived_amount", BigInteger),
    Column("created_at", Text),
    Column("updated_at", Text),
)


class TransactionEffective(Base):
    __table__ = transactions_effective


class ReadOnlyViewError(RuntimeError):
    pass


@event.listens_for(Session, "before_flush")
def _refuse_view_writes(session, _flush_context, _instances):
    for obj in (*session.new, *session.dirty, *session.deleted):
        if isinstance(obj, TransactionEffective):
            raise ReadOnlyViewError(
                "transactions_effective is read-only: write transactions through the "
                "pipeline and user changes as transaction_edits rows."
            )
