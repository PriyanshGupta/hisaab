"""jobs.kind gains 'backup'.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-21

Slice 25. tech-design §6.7 runs the daily backup through the same queue as
every job; §3.4's kind list omits it, as it omitted 'cleanup' (0004),
'backfill' (0007) and 'sender_backfill' (0011). Downgrade deletes its rows
(job history only) so the old CHECK can return.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0013'
down_revision: Union[str, Sequence[str], None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_KINDS: str = ("'sync', 'resync', 'sweep', 'statement_merge', 'reprocess_normalization', 'cleanup', 'backfill', "
                  "'sender_backfill'")
NEW_KINDS: str = OLD_KINDS + ", 'backup'"


def _set_kinds(kinds: str) -> None:
    # The rebuild would copy ix_jobs_user_state without its DESC, so it is recreated by hand.
    op.drop_index('ix_jobs_user_state', table_name='jobs')
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('ck_jobs_kind'), type_='check')  # f(): already the full name
        batch_op.create_check_constraint('kind', f"kind IN ({kinds})")
    op.create_index('ix_jobs_user_state', 'jobs', ['user_id', 'state', sa.literal_column('created_at DESC')])


def upgrade() -> None:
    _set_kinds(NEW_KINDS)


def downgrade() -> None:
    op.execute("DELETE FROM jobs WHERE kind = 'backup'")
    _set_kinds(OLD_KINDS)
