"""Numbers the user says aren't their accounts.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-19

User, 2026-10-19: a FASTag wallet isn't an account; its toll payments are just
expenses. Review's "Not my account" writes one row here per number; the leg
stays outside the user's accounts and review stops asking which account it is.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('external_identifiers',
    sa.Column('user_id', sa.Text(), nullable=False),
    sa.Column('identifier', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_external_identifiers_user_id_users')),
    sa.PrimaryKeyConstraint('user_id', 'identifier', name=op.f('pk_external_identifiers'))
    )


def downgrade() -> None:
    op.drop_table('external_identifiers')
