"""Balances and the reconciliation check (tech-design §8.3, §8.4; PRD §4.11, §7.10).

mark_changed: for writes the ORM can't see (bulk DELETE/UPDATE statements), tell the commit hook in
balances/checks.py that this user's balances may have moved, so the check runs before the commit.
"""

from sqlalchemy.orm import Session

BALANCE_USERS: str = "balance_users"  # session.info key: users whose balances this transaction may change


def mark_changed(session: Session, user_id: str) -> None:
    session.info.setdefault(BALANCE_USERS, set()).add(user_id)
