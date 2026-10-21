"""Two global tables for the admin screens (slice 24).

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-21

correction_dismissals: captured payee corrections (PRD §5.6) are read from the
users' payee edits; only the admin's "dismiss" needs storing, keyed by the raw
text squashed and the payee it was corrected to.

resync_notices: tech-design §5.2's parser-change prompts. Editing, approving or
updating a shipped template writes one, with the date range of the emails that
template read; the admin banner offers "Re-sync that range".

Both are global, like normalization_patterns and parser_templates; tech-design
§3 doesn't list them, as it didn't list 0004/0007's job kinds.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0012'
down_revision: Union[str, Sequence[str], None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('correction_dismissals',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('raw_key', sa.Text(), nullable=False),
    sa.Column('payee_id', sa.Text(), nullable=False),
    sa.Column('dismissed_by', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['dismissed_by'], ['users.id'], name=op.f('fk_correction_dismissals_dismissed_by_users')),
    sa.ForeignKeyConstraint(['payee_id'], ['payees.id'], name=op.f('fk_correction_dismissals_payee_id_payees')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_correction_dismissals')),
    sa.UniqueConstraint('raw_key', 'payee_id', name=op.f('uq_correction_dismissals_raw_key_payee_id'))
    )
    op.create_table('resync_notices',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('template_id', sa.Text(), nullable=True),
    sa.Column('template_name', sa.Text(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('range_from', sa.Text(), nullable=False),
    sa.Column('range_to', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.Column('resolved_at', sa.Text(), nullable=True),
    sa.CheckConstraint("reason IN ('edited', 'approved', 'shipped_updated')", name=op.f('ck_resync_notices_reason')),
    sa.ForeignKeyConstraint(['template_id'], ['parser_templates.id'], name=op.f('fk_resync_notices_template_id_parser_templates')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_resync_notices'))
    )


def downgrade() -> None:
    op.drop_table('resync_notices')
    op.drop_table('correction_dismissals')
