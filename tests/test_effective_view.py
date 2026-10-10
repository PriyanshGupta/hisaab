"""transactions_effective: an edit row's value wins over the parser's value (tech-design §3.5, amended).

The key amendment: winning is decided by the existence of the edit row, not by
COALESCE, so an edit to NULL really clears the field.
"""

import pytest
from sqlalchemy import text

from tests import factories as f

VIEW = "transactions_effective"


def effective(conn, txn_id, view=VIEW):
    return conn.execute(text(f"SELECT * FROM {view} WHERE id = :id"), {"id": txn_id}).mappings().one()


@pytest.fixture
def txn(conn):
    """A transaction with every overlaid field populated by the 'parser'."""
    u = f.user(conn)["id"]
    a, b = f.account(conn, u)["id"], f.account(conn, u)["id"]
    return f.transaction(
        conn, user_id=u, amount=10000, kind="expense", from_account_id=a, to_account_id=b,
        payee_id=f.payee(conn)["id"], transaction_date="2026-08-20",
    )


def edit(conn, txn, field, new_value, prior_value=None):
    f.transaction_edit(conn, txn["id"], txn["user_id"], field=field, new_value=new_value, prior_value=prior_value)


def test_without_edits_the_view_shows_the_parser_values(conn, txn):
    row = effective(conn, txn["id"])
    for column in ("amount", "kind", "from_account_id", "to_account_id", "payee_id", "transaction_date"):
        assert row[column] == txn[column], column
    assert type(row["amount"]) is int
    assert row["category_override_id"] is None


def test_amount_edit_wins_over_the_base_table(conn, txn):
    edit(conn, txn, "amount", "25000", prior_value="10000")
    row = effective(conn, txn["id"])
    assert row["amount"] == 25000
    assert type(row["amount"]) is int  # cast back to integer paise, not the TEXT stored in the edit
    assert row["derived_amount"] == 10000  # the parser's value stays visible for undo/conflict review


@pytest.mark.parametrize("field,new_value", [
    ("kind", "refund"),
    ("transaction_date", "2026-08-31"),
])
def test_scalar_edits_win(conn, txn, field, new_value):
    edit(conn, txn, field, new_value)
    assert effective(conn, txn["id"])[field] == new_value


def test_kind_edit_keeps_derived_kind(conn, txn):
    edit(conn, txn, "kind", "refund")
    row = effective(conn, txn["id"])
    assert (row["kind"], row["derived_kind"]) == ("refund", "expense")


@pytest.mark.parametrize("field", ["from_account_id", "to_account_id"])
def test_account_edits_win(conn, txn, field):
    other = f.account(conn, txn["user_id"])["id"]
    edit(conn, txn, field, other)
    assert effective(conn, txn["id"])[field] == other


def test_payee_edit_wins(conn, txn):
    other = f.payee(conn)["id"]
    edit(conn, txn, "payee_id", other)
    assert effective(conn, txn["id"])["payee_id"] == other


def test_category_override_and_acknowledgement_come_through(conn, txn):
    cat = f.category(conn, txn["user_id"])["id"]
    edit(conn, txn, "category_override_id", cat)
    edit(conn, txn, "review_acknowledged", "10000")
    row = effective(conn, txn["id"])
    assert (row["category_override_id"], row["review_acknowledged"]) == (cat, "10000")


def test_edit_to_null_clears_the_field_instead_of_falling_back(conn, txn):
    # The user marks the to-leg external; the parser had resolved it to an account.
    edit(conn, txn, "to_account_id", None, prior_value=txn["to_account_id"])
    assert effective(conn, txn["id"])["to_account_id"] is None


def test_payee_edit_to_null_clears_the_payee(conn, txn):
    edit(conn, txn, "payee_id", None, prior_value=txn["payee_id"])
    assert effective(conn, txn["id"])["payee_id"] is None


def test_assigning_a_leg_by_edit_clears_its_unresolved_identifier(conn):
    t = f.transaction(conn, from_account_id=None, unresolved_from_identifier="XX8830")
    assert effective(conn, t["id"])["unresolved_from_identifier"] == "XX8830"
    acct = f.account(conn, t["user_id"])["id"]
    edit(conn, t, "from_account_id", acct)
    row = effective(conn, t["id"])
    assert (row["from_account_id"], row["unresolved_from_identifier"]) == (acct, None)


def test_edits_on_one_transaction_do_not_leak_to_another(conn, txn):
    other = f.transaction(conn, user_id=txn["user_id"], amount=777)
    edit(conn, txn, "amount", "25000")
    assert effective(conn, other["id"])["amount"] == 777


def test_view_has_one_row_per_transaction_regardless_of_edit_count(conn, txn):
    for field, value in [("amount", "1"), ("kind", "refund"), ("payee_id", None), ("transaction_date", "2026-08-01")]:
        edit(conn, txn, field, value)
    assert conn.execute(text(f"SELECT count(*) FROM {VIEW} WHERE id = :id"), {"id": txn["id"]}).scalar() == 1


def test_null_edit_test_distinguishes_the_original_coalesce_view(conn, txn):
    """Mutation check: tech-design's original COALESCE view fails the NULL-edit case.

    Creates §3.5's view verbatim under another name and shows it returns the
    parser's account where the amended view correctly returns NULL.
    """
    conn.execute(text("""
        CREATE TEMP VIEW coalesce_view AS
        SELECT t.id, COALESCE(e.to_account_id, t.to_account_id) AS to_account_id
        FROM transactions t LEFT JOIN (
          SELECT transaction_id, MAX(CASE WHEN field='to_account_id' THEN new_value END) AS to_account_id
          FROM transaction_edits GROUP BY transaction_id
        ) e ON e.transaction_id = t.id
    """))
    edit(conn, txn, "to_account_id", None, prior_value=txn["to_account_id"])
    assert effective(conn, txn["id"], view="coalesce_view")["to_account_id"] == txn["to_account_id"]  # the bug
    assert effective(conn, txn["id"])["to_account_id"] is None  # the fix
