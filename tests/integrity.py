"""Cross-user integrity scan (tech-design §3.9; build-plan "tests that must exist").

SQLite can't enforce that a row only points at the same user's data, so this
scan looks for any row whose owner disagrees with the owner of what it points
at. Each check is one SQL query returning offending row ids; an empty result
everywhere means no family member's data leaks into another's.

test_cross_user_integrity.py fails if a foreign key between user-scoped tables
has no check here, so a new table or column can't silently escape the scan.
"""

from dataclasses import dataclass

from sqlalchemy import Connection, text


@dataclass(frozen=True)
class Check:
    name: str
    covers: tuple[tuple[str, str], ...]  # (table, column) foreign keys this check verifies
    sql: str


def _owner_matches(table: str, column: str, target: str, owner_col: str = "user_id") -> Check:
    """Rows of `table` whose `column` points at a `target` row owned by someone else."""
    return Check(
        name=f"{table}.{column} points at another user's {target}",
        covers=((table, column),),
        sql=f"""SELECT x.rowid FROM {table} x JOIN {target} t ON t.id = x.{column}
                WHERE x.{owner_col} <> t.user_id""",
    )


CHECKS: list[Check] = [
    _owner_matches("account_identifiers", "account_id", "accounts"),
    _owner_matches("transactions", "from_account_id", "accounts"),
    _owner_matches("transactions", "to_account_id", "accounts"),
    _owner_matches("transactions", "primary_source_id", "sources"),
    _owner_matches("transactions", "reverses_transaction_id", "transactions"),
    _owner_matches("transactions", "refunds_transaction_id", "transactions"),
    _owner_matches("transaction_edits", "transaction_id", "transactions"),
    _owner_matches("payee_categories", "category_id", "categories"),
    _owner_matches("payee_suggestions", "category_id", "categories"),
    _owner_matches("raw_payee_suggestions", "category_id", "categories"),
    _owner_matches("balance_observations", "account_id", "accounts"),
    _owner_matches("balance_observations", "source_id", "sources"),
    _owner_matches("balance_deltas", "account_id", "accounts"),
    _owner_matches("balance_deltas", "observation_id", "balance_observations"),
    _owner_matches("balance_deltas", "adjustment_transaction_id", "transactions"),
    _owner_matches("statement_staging", "account_id", "accounts"),
    Check(
        name="transaction_sources links a transaction to another user's source",
        covers=(("transaction_sources", "transaction_id"), ("transaction_sources", "source_id")),
        sql="""SELECT ts.rowid FROM transaction_sources ts
               JOIN transactions t ON t.id = ts.transaction_id
               JOIN sources s ON s.id = ts.source_id
               WHERE t.user_id <> s.user_id""",
    ),
    # Edits store ids as text in new_value, so the schema can't see these references.
    Check(
        name="an edit moves a transaction onto another user's account",
        covers=(),
        sql="""SELECT e.rowid FROM transaction_edits e JOIN accounts a ON a.id = e.new_value
               WHERE e.field IN ('from_account_id', 'to_account_id') AND a.user_id <> e.user_id""",
    ),
    Check(
        name="an edit gives a transaction another user's category",
        covers=(),
        sql="""SELECT e.rowid FROM transaction_edits e JOIN categories c ON c.id = e.new_value
               WHERE e.field = 'category_override_id' AND c.user_id <> e.user_id""",
    ),
]


def cross_user_violations(conn: Connection) -> dict[str, list[int]]:
    """{check name: offending rowids} for every check that found something."""
    found: dict[str, list[int]] = {}
    for check in CHECKS:
        rows: list[int] = [r[0] for r in conn.execute(text(check.sql))]
        if rows:
            found[check.name] = rows
    return found
