"""Seed the default categories for users created before category seeding existed.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-11

From slice 7, every new user gets PRD §5.3's ten default categories when they
are created (hisaab.auth.users.create_user). Users created earlier have none;
this gives them the same ten. Users who already have any category are left
alone. The list is copied here, not imported: migrations are frozen.

Downgrade removes nothing. It can't tell seeded defaults from a user's own
categories once they are in use, and deleting categories would break rules.
"""
import uuid
from datetime import UTC, datetime, timedelta
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_CATEGORIES = (
    "Food & Dining", "Groceries", "Transport", "Bills & Utilities", "Rent",
    "Shopping", "Health", "Entertainment", "Lending", "Other",
)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def upgrade() -> None:
    conn = op.get_bind()
    users_without = [r[0] for r in conn.execute(sa.text(
        "SELECT id FROM users WHERE NOT EXISTS (SELECT 1 FROM categories c WHERE c.user_id = users.id)"
    ))]
    categories = sa.table("categories", sa.column("id", sa.Text), sa.column("user_id", sa.Text),
                          sa.column("name", sa.Text), sa.column("created_at", sa.Text))
    end = datetime.now(UTC)
    last = len(DEFAULT_CATEGORIES) - 1
    rows = [
        {"id": str(uuid.uuid4()), "user_id": user_id, "name": name,
         "created_at": _iso(end - timedelta(milliseconds=last - i))}  # 1 ms apart, ending now
        for user_id in users_without
        for i, name in enumerate(DEFAULT_CATEGORIES)
    ]
    if rows:
        op.bulk_insert(categories, rows)


def downgrade() -> None:
    pass  # see module docstring
