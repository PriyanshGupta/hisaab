"""Slice 17: who wrote a payee rule, auto-categorize's memory, an optional opening balance, its threshold.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-12

- payee_categories.origin ('user' | 'llm') and batch_id: a rule thought about
  versus a machine guess, and which auto-categorize run wrote it (undo).
- payee_suggestions: every auto-categorize answer, confident or not, so a
  payee already asked about is never sent again for the same category list.
- accounts.opening_balance / opening_balance_date nullable, both or neither
  (ruling 2026-10-12, amending PRD §4.11): an account may have no anchor.
- setting auto_categorize_min_confidence_percent = 80 (settings hold integers).

Batch mode rebuilds accounts; its partial unique index is restored from its own
stored SQL, as 0006 did for transactions. Downgrade refuses while an account has
no opening balance rather than invent one.
"""
import json
from datetime import UTC, datetime
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

THRESHOLD: str = "auto_categorize_min_confidence_percent"
ANCHOR_CHECK: str = "(opening_balance IS NULL) = (opening_balance_date IS NULL)"


def _indexes(table: str) -> list[tuple[str, str]]:
    return [(n, s) for n, s in op.get_bind().execute(sa.text(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = :t AND sql IS NOT NULL"), {"t": table})]


def _restore(indexes: list[tuple[str, str]]) -> None:
    for name, sql in indexes:  # the rebuild drops partial WHERE clauses; put each back exactly
        op.execute(f'DROP INDEX IF EXISTS "{name}"')
        op.execute(sql)


def _now() -> str:
    now: datetime = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def upgrade() -> None:
    indexes: list[tuple[str, str]] = _indexes("accounts")
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.alter_column('opening_balance', existing_type=sa.BigInteger(), nullable=True)
        batch_op.alter_column('opening_balance_date', existing_type=sa.Text(), nullable=True)
        batch_op.create_check_constraint('anchor_both_or_neither', ANCHOR_CHECK)
    _restore(indexes)
    with op.batch_alter_table('payee_categories', schema=None) as batch_op:
        batch_op.add_column(sa.Column('origin', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('batch_id', sa.Text(), nullable=True))
        batch_op.create_check_constraint('origin', "origin IN ('user', 'llm')")
    op.create_table('payee_suggestions',
    sa.Column('user_id', sa.Text(), nullable=False),
    sa.Column('payee_id', sa.Text(), nullable=False),
    sa.Column('category_id', sa.Text(), nullable=True),
    sa.Column('confidence_percent', sa.Integer(), nullable=False),
    sa.Column('categories_hash', sa.Text(), nullable=False),
    sa.Column('batch_id', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['category_id'], ['categories.id'], name=op.f('fk_payee_suggestions_category_id_categories')),
    sa.ForeignKeyConstraint(['payee_id'], ['payees.id'], name=op.f('fk_payee_suggestions_payee_id_payees')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_payee_suggestions_user_id_users')),
    sa.PrimaryKeyConstraint('user_id', 'payee_id', name=op.f('pk_payee_suggestions'))
    )
    op.execute(sa.text("INSERT INTO settings (key, user_id, value, updated_at) VALUES (:k, '', :v, :at)")
               .bindparams(k=THRESHOLD, v=json.dumps(80), at=_now()))


def downgrade() -> None:
    open_anchor: int = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM accounts WHERE opening_balance IS NULL")).scalar_one()
    if open_anchor:
        raise RuntimeError(f"{open_anchor} account{' has' if open_anchor == 1 else 's have'} no opening balance; "
                           "add one before downgrading")
    op.execute(sa.text("DELETE FROM settings WHERE key = :k").bindparams(k=THRESHOLD))
    op.drop_table('payee_suggestions')
    with op.batch_alter_table('payee_categories', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('ck_payee_categories_origin'), type_='check')
        batch_op.drop_column('batch_id')
        batch_op.drop_column('origin')
    indexes: list[tuple[str, str]] = _indexes("accounts")
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('ck_accounts_anchor_both_or_neither'), type_='check')
        batch_op.alter_column('opening_balance_date', existing_type=sa.Text(), nullable=False)
        batch_op.alter_column('opening_balance', existing_type=sa.BigInteger(), nullable=False)
    _restore(indexes)
