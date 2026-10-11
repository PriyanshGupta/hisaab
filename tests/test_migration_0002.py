"""Migration 0002: uniqueness of last4 and identifiers applies among ACTIVE accounts only.

Ruling 2026-10-11: archiving frees an account's identifiers, so a UPI ID
re-linked to a new bank account, or a replacement account, can be added again.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from hisaab.models import utc_now
from tests import factories as f


def test_archived_accounts_last4_can_be_reused(conn):
    old = f.account(conn, last4="5512", archived_at=utc_now())
    f.account(conn, old["user_id"], last4="5512")  # the replacement account


def test_two_active_accounts_cannot_share_last4(conn):
    a = f.account(conn, last4="5512")
    with pytest.raises(IntegrityError, match="UNIQUE constraint failed: accounts.user_id, accounts.last4"):
        f.account(conn, a["user_id"], last4="5512")


def test_retired_identifier_can_be_reused(conn):
    old = f.account_identifier(conn, kind="vpa", identifier="asha@ybl", retired_at=utc_now())
    new_account = f.account(conn, old["user_id"])
    f.account_identifier(conn, new_account["id"], old["user_id"], kind="vpa", identifier="asha@ybl")


def test_two_active_identifiers_cannot_collide(conn):
    i = f.account_identifier(conn, kind="vpa", identifier="asha@ybl")
    with pytest.raises(IntegrityError, match="UNIQUE constraint failed: account_identifiers"):
        f.account_identifier(conn, i["account_id"], i["user_id"], kind="vpa", identifier="asha@ybl")


@pytest.mark.parametrize("name,table,predicate", [
    ("uq_accounts_active_last4", "accounts", "archived_at IS NULL"),
    ("uq_account_identifiers_active", "account_identifiers", "retired_at IS NULL"),
])
def test_active_uniqueness_is_a_partial_unique_index(conn, name, table, predicate):
    row = next((r for r in conn.execute(text(f"PRAGMA index_list({table})")) if r[1] == name), None)
    assert row is not None, f"{name} missing"
    assert row[2] == 1 and row[4] == 1  # unique, partial
    sql = conn.execute(text("SELECT sql FROM sqlite_master WHERE name = :n"), {"n": name}).scalar()
    assert predicate in sql


def test_the_old_table_level_unique_constraints_are_gone(conn):
    ddl = " ".join(r[0] for r in conn.execute(text(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name IN ('accounts', 'account_identifiers')")))
    assert "uq_accounts_user_id_last4" not in ddl
    assert "uq_account_identifiers_user_id_kind_identifier" not in ddl
