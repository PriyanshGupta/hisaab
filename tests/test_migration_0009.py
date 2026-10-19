"""Migration 0009: numbers the user says aren't their accounts (a FASTag wallet; user, 2026-10-19)."""

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from tests import factories as f

INSERT: str = "INSERT INTO external_identifiers (user_id, identifier, created_at) VALUES (:u, :i, 'x')"


def test_one_row_per_user_and_number(conn: Connection) -> None:
    u: str = f.user(conn)["id"]
    conn.execute(text(INSERT), {"u": u, "i": "5678"})
    conn.execute(text(INSERT), {"u": f.user(conn)["id"], "i": "5678"})  # another user's own say
    with pytest.raises(IntegrityError):
        conn.execute(text(INSERT), {"u": u, "i": "5678"})
