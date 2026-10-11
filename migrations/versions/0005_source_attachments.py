"""sources.attachments: attachment metadata captured at sync.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-11

Ruling 2026-10-11 (slice 13). tech-design §5.1 has sync capture each email's
attachment metadata (filename, size, MIME type; never the file) so the app can
point at likely statements, but §3 gave it no column. JSON list, NULL when the
email has no attachments; kept when the content is discarded.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('sources', schema=None) as batch_op:
        batch_op.add_column(sa.Column('attachments', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('sources', schema=None) as batch_op:
        batch_op.drop_column('attachments')
