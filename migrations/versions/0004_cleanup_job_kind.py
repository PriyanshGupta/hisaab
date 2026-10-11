"""jobs.kind gains 'cleanup'.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-11

Ruling 2026-10-11 (slice 12). tech-design §6.7 runs the daily cleanup through
the job queue, "one code path, no special cases", so a cleanup is a jobs row
like any other; §3.4's kind list left it out. Batch mode rebuilds the table to
change the CHECK (SQLite cannot alter one in place).

Downgrade deletes cleanup rows (job history only) so the old CHECK can return.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_KINDS: str = "'sync', 'resync', 'sweep', 'statement_merge', 'reprocess_normalization'"
NEW_KINDS: str = OLD_KINDS + ", 'cleanup'"


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
    op.execute("DELETE FROM jobs WHERE kind = 'cleanup'")
    _set_kinds(OLD_KINDS)
