"""Auto-categorize's memory for payees not yet named.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-19

User, 2026-10-19: naming payees is optional, so auto-categorize also files
payees known only by the text alerts printed (JioHotstar). Its answers for
those are remembered here, keyed by that text, so a second click asks nothing.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0010'
down_revision: Union[str, Sequence[str], None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('raw_payee_suggestions',
    sa.Column('user_id', sa.Text(), nullable=False),
    sa.Column('payee_raw', sa.Text(), nullable=False),
    sa.Column('category_id', sa.Text(), nullable=True),
    sa.Column('confidence_percent', sa.Integer(), nullable=False),
    sa.Column('categories_hash', sa.Text(), nullable=False),
    sa.Column('batch_id', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['category_id'], ['categories.id'], name=op.f('fk_raw_payee_suggestions_category_id_categories')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_raw_payee_suggestions_user_id_users')),
    sa.PrimaryKeyConstraint('user_id', 'payee_raw', name=op.f('pk_raw_payee_suggestions'))
    )


def downgrade() -> None:
    op.drop_table('raw_payee_suggestions')
