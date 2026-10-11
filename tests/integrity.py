"""Cross-user integrity scan (tech-design §3.9; build-plan "tests that must exist").

SQLite can't enforce that a row only points at the same user's data, so this
scan looks for any row whose user_id disagrees with the user of what it points
at. Each check is one SQL query that returns offending rows; an empty result
everywhere means no family member's data leaks into another's.

Add a check here whenever a table gains a cross-row reference: slice 6 adds
transactions -> accounts/payees/categories, slice 7 payee_categories, and so on.
"""

from sqlalchemy import text

CHECKS = {
    "identifier belongs to another user's account": """
        SELECT i.id FROM account_identifiers i
        JOIN accounts a ON a.id = i.account_id
        WHERE i.user_id <> a.user_id
    """,
}


def cross_user_violations(conn) -> dict[str, list[str]]:
    """{check name: offending row ids} for every check that found something."""
    found = {}
    for name, sql in CHECKS.items():
        rows = [r[0] for r in conn.execute(text(sql))]
        if rows:
            found[name] = rows
    return found
