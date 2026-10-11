"""A transfer may leave its paying account unknown.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-11

Ruling 2026-10-11 (slice 15). ICICI and Scapia email "payment received" on the
card but never say which account paid it. That is a card bill payment, so a
transfer, not income and not a refund; its from-leg stays NULL until a matching
bank debit (or the user, in review) fills it in. §3's CHECK demanded both legs.

Batch mode rebuilds the table. The view over it is dropped first and restored
from its own stored SQL; indexes the rebuild loses are restored the same way.
Downgrade refuses while any such transfer exists rather than reclassify it.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD: tuple[str, str] = ("transfer_has_two_distinct_legs", "kind <> 'transfer' OR (from_account_id IS NOT NULL "
                        "AND to_account_id IS NOT NULL AND from_account_id <> to_account_id)")
NEW: tuple[str, str] = ("transfer_legs", "kind <> 'transfer' OR (to_account_id IS NOT NULL "
                        "AND (from_account_id IS NULL OR from_account_id <> to_account_id))")


def _swap(old: tuple[str, str], new: tuple[str, str]) -> None:
    bind: sa.Connection = op.get_bind()
    view: str = bind.execute(sa.text(
        "SELECT sql FROM sqlite_master WHERE type = 'view' AND name = 'transactions_effective'")).scalar_one()
    indexes: list[tuple[str, str]] = [(n, s) for n, s in bind.execute(sa.text(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'transactions' AND sql IS NOT NULL"))]
    op.execute("DROP VIEW transactions_effective")
    with op.batch_alter_table('transactions', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f(f"ck_transactions_{old[0]}"), type_="check")  # f(): already the full name
        batch_op.create_check_constraint(new[0], new[1])
    for name, sql in indexes:  # the rebuild drops partial WHERE clauses; put each back exactly
        op.execute(f'DROP INDEX IF EXISTS "{name}"')
        op.execute(sql)
    op.execute(view)


def upgrade() -> None:
    _swap(OLD, NEW)


def downgrade() -> None:
    open_payer: int = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM transactions WHERE kind = 'transfer' AND from_account_id IS NULL")).scalar_one()
    if open_payer:
        raise RuntimeError(f"{open_payer} transfer{' has' if open_payer == 1 else 's have'} no paying account; "
                           "assign one before downgrading")
    _swap(NEW, OLD)
