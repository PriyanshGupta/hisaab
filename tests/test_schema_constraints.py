"""Every constraint the migrated schema promises, tested against the real migration.

Each rejection test is paired with a positive control (the valid version is
accepted), and asserts the *named* constraint that fired. A rejection for
the wrong reason would therefore fail the test.
"""

import re
from contextlib import AbstractContextManager
from typing import Any

import pytest
from sqlalchemy import Connection, delete, select, text
from sqlalchemy.exc import IntegrityError

from tests import factories as f
from tests.factories import FACTORIES, T

# (table, column, constraint name suffix) for every enum-style CHECK.
ENUM_CHECKS: list[tuple[str, str, str]] = [
    ("users", "role", "role"),
    ("accounts", "type", "type"),
    ("account_identifiers", "kind", "kind"),
    ("account_identifiers", "origin", "origin"),
    ("sources", "kind", "kind"),
    ("sources", "processing_state", "processing_state"),
    ("transactions", "kind", "kind"),
    ("transactions", "status", "status"),
    ("payees", "type", "type"),
    ("balance_observations", "source", "source"),
    ("balance_deltas", "status", "status"),
    ("parser_templates", "artifact_type", "artifact_type"),
    ("parser_templates", "origin", "origin"),
    ("parser_templates", "status", "status"),
    ("extraction_cache", "outcome", "outcome"),
    ("format_sightings", "state", "state"),
    ("normalization_patterns", "match_type", "match_type"),
    ("normalization_patterns", "origin", "origin"),
    ("senders", "status", "status"),
    ("gmail_connections", "status", "status"),
    ("jobs", "kind", "kind"),
    ("jobs", "state", "state"),
    ("payee_categories", "origin", "origin"),
]


def rejected(conn: Connection, constraint: str) -> AbstractContextManager[pytest.ExceptionInfo[IntegrityError]]:
    """Context manager: the statement must fail on exactly this constraint."""
    return pytest.raises(IntegrityError, match=constraint)


# --- positive controls ------------------------------------------------------

@pytest.mark.parametrize("table", sorted(FACTORIES))
def test_factory_inserts_a_valid_row(conn: Connection, table: str) -> None:
    FACTORIES[table](conn)
    assert conn.execute(select(text("count(*)")).select_from(T[table])).scalar_one() >= 1


# --- enum CHECKs ------------------------------------------------------------

@pytest.mark.parametrize("table,column,name", ENUM_CHECKS, ids=[f"{t}.{c}" for t, c, _ in ENUM_CHECKS])
def test_enum_check_rejects_unknown_value(conn: Connection, table: str, column: str, name: str) -> None:
    with rejected(conn, f"CHECK constraint failed: ck_{table}_{name}"):
        FACTORIES[table](conn, **{column: "not_a_valid_value"})


def test_every_enum_check_is_covered(conn: Connection) -> None:
    # Guards the list above: any new IN (...) CHECK in the schema must be added to it.
    ddl: str = " ".join(r[0] for r in conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table'")))
    enum_names: set[Any] = set(re.findall(r"CONSTRAINT (ck_\w+) CHECK \(\w+ IN \(", ddl))
    assert enum_names == {f"ck_{t}_{n}" for t, _, n in ENUM_CHECKS}


# --- transactions: amount ---------------------------------------------------

@pytest.mark.parametrize("amount", [-1, -10000, 0])
def test_amount_must_be_positive(conn: Connection, amount: int) -> None:
    with rejected(conn, "CHECK constraint failed: ck_transactions_amount_positive"):
        f.transaction(conn, amount=amount)


def test_positive_amount_accepted(conn: Connection) -> None:
    f.transaction(conn, amount=1)


# --- transactions: transfer legs --------------------------------------------

@pytest.fixture
def two_accounts(conn: Connection) -> tuple[str, str, str]:
    u: str = f.user(conn)["id"]
    return u, f.account(conn, u)["id"], f.account(conn, u)["id"]


def test_transfer_with_two_distinct_legs_accepted(conn: Connection, two_accounts: tuple[str, str, str]) -> None:
    u, a, b = two_accounts
    f.transaction(conn, user_id=u, kind="transfer", from_account_id=a, to_account_id=b)


def test_transfer_with_the_paying_account_unknown_accepted(conn: Connection, two_accounts: tuple[str, str, str]) -> None:
    u, _, b = two_accounts  # a card payment received from an account the app can't see (ruling 2026-10-11)
    f.transaction(conn, user_id=u, kind="transfer", from_account_id=None, to_account_id=b)


@pytest.mark.parametrize("legs", ["from_only", "neither", "same_account"])
def test_transfer_without_a_distinct_destination_rejected(
    conn: Connection, two_accounts: tuple[str, str, str], legs: str,
) -> None:
    u, a, _ = two_accounts
    from_id, to_id = {"from_only": (a, None), "neither": (None, None), "same_account": (a, a)}[legs]
    with rejected(conn, "CHECK constraint failed: ck_transactions_transfer_legs"):
        f.transaction(conn, user_id=u, kind="transfer", from_account_id=from_id, to_account_id=to_id)


def test_one_legged_expense_is_fine(conn: Connection, two_accounts: tuple[str, str, str]) -> None:
    u, a, _ = two_accounts
    f.transaction(conn, user_id=u, kind="expense", from_account_id=a, to_account_id=None)


# --- transactions: resolved XOR unresolved per leg --------------------------

@pytest.mark.parametrize("leg", ["from", "to"])
def test_leg_both_resolved_and_unresolved_rejected(
    conn: Connection, two_accounts: tuple[str, str, str], leg: str,
) -> None:
    u, a, _ = two_accounts
    with rejected(conn, f"CHECK constraint failed: ck_transactions_{leg}_leg_not_both_resolved_and_unresolved"):
        f.transaction(conn, user_id=u, **{f"{leg}_account_id": a, f"unresolved_{leg}_identifier": "XX1234"})


@pytest.mark.parametrize("leg", ["from", "to"])
def test_unresolved_only_leg_accepted(conn: Connection, leg: str) -> None:
    f.transaction(conn, **{f"unresolved_{leg}_identifier": "XX1234"})


# --- transaction_edits ------------------------------------------------------

@pytest.mark.parametrize("field", ["amount", "kind", "transaction_date", "review_acknowledged"])
def test_edit_cannot_null_a_required_field(conn: Connection, field: str) -> None:
    with rejected(conn, "CHECK constraint failed: ck_transaction_edits_required_field_not_null"):
        f.transaction_edit(conn, field=field, new_value=None)


@pytest.mark.parametrize("field", ["to_account_id", "from_account_id", "payee_id", "category_override_id"])
def test_edit_may_null_an_optional_field(conn: Connection, field: str) -> None:
    f.transaction_edit(conn, field=field, new_value=None)


# --- foreign keys -----------------------------------------------------------

def test_foreign_keys_are_enforced(conn: Connection) -> None:
    src: dict[str, Any] = f.source(conn)
    with rejected(conn, "FOREIGN KEY constraint failed"):
        f.transaction(conn, user_id="no-such-user", source_id=src["id"])


def test_deleting_a_transaction_cascades_to_its_source_links(conn: Connection) -> None:
    link: dict[str, Any] = f.transaction_source(conn)
    conn.execute(delete(T["transactions"]).where(T["transactions"].c.id == link["transaction_id"]))
    remaining: Any | None = conn.execute(
        select(text("count(*)")).select_from(T["transaction_sources"]).where(
            T["transaction_sources"].c.transaction_id == link["transaction_id"]
        )
    ).scalar()
    assert remaining == 0


def test_deleting_a_transaction_with_edits_is_refused_and_the_edit_survives(conn: Connection) -> None:
    edit: dict[str, Any] = f.transaction_edit(conn)
    with rejected(conn, "FOREIGN KEY constraint failed"):
        conn.execute(delete(T["transactions"]).where(T["transactions"].c.id == edit["transaction_id"]))
    assert conn.execute(select(T["transaction_edits"].c.id).where(T["transaction_edits"].c.id == edit["id"])).scalar()


# --- UNIQUE -----------------------------------------------------------------

def test_unique_last4_per_user(conn: Connection) -> None:
    a: dict[str, Any] = f.account(conn, last4="5512")
    with rejected(conn, "UNIQUE constraint failed: accounts.user_id, accounts.last4"):
        f.account(conn, a["user_id"], last4="5512")
    f.account(conn, last4="5512")  # another user may have the same last4


def test_multiple_cash_accounts_with_null_last4_allowed(conn: Connection) -> None:
    u: str = f.user(conn)["id"]
    f.account(conn, u, type="cash", last4=None)
    f.account(conn, u, type="cash", last4=None)


def test_unique_derived_key_per_user(conn: Connection) -> None:
    t: dict[str, Any] = f.transaction(conn)
    with rejected(conn, "UNIQUE constraint failed: transactions.user_id, transactions.derived_key"):
        f.transaction(conn, user_id=t["user_id"], source_id=t["primary_source_id"], derived_key=t["derived_key"])


def test_unique_gmail_message_per_user(conn: Connection) -> None:
    s: dict[str, Any] = f.source(conn)
    with rejected(conn, "UNIQUE constraint failed: sources.user_id, sources.external_message_id"):
        f.source(conn, s["user_id"], external_message_id=s["external_message_id"])


def test_sources_without_message_id_never_collide(conn: Connection) -> None:
    u: str = f.user(conn)["id"]
    f.source(conn, u, kind="manual", external_message_id=None)
    f.source(conn, u, kind="statement", external_message_id=None)


def test_unique_ingest_seq(conn: Connection) -> None:
    s: dict[str, Any] = f.source(conn)
    with rejected(conn, "UNIQUE constraint failed: sources.ingest_seq"):
        f.source(conn, ingest_seq=s["ingest_seq"])


def test_unique_identifier_per_user_and_kind(conn: Connection) -> None:
    i: dict[str, Any] = f.account_identifier(conn, identifier="upi@ybl", kind="vpa")
    with rejected(conn, "UNIQUE constraint failed: account_identifiers.user_id"):
        f.account_identifier(conn, i["account_id"], i["user_id"], identifier="upi@ybl", kind="vpa")


def test_unique_category_name_per_user(conn: Connection) -> None:
    c: dict[str, Any] = f.category(conn, name="Groceries")
    with rejected(conn, "UNIQUE constraint failed: categories.user_id, categories.name"):
        f.category(conn, c["user_id"], name="Groceries")


def test_one_edit_row_per_transaction_field(conn: Connection) -> None:
    e: dict[str, Any] = f.transaction_edit(conn, field="amount")
    with rejected(conn, "UNIQUE constraint failed: transaction_edits.transaction_id, transaction_edits.field"):
        f.transaction_edit(conn, e["transaction_id"], e["user_id"], field="amount", new_value="1")


def test_unique_payee_name(conn: Connection) -> None:
    f.payee(conn, normalized_name="Swiggy")
    with rejected(conn, "UNIQUE constraint failed: payees.normalized_name"):
        f.payee(conn, normalized_name="Swiggy")


def test_unique_sender_address(conn: Connection) -> None:
    f.sender(conn, email_address="alerts@example.test")
    with rejected(conn, "UNIQUE constraint failed: senders.email_address"):
        f.sender(conn, email_address="alerts@example.test")
