"""statement_staging: an upload waiting for its preview to be confirmed (tech-design §7 step 4).

    parsed_rows       the rows the interpreter read, as StatementRow dicts
    row_dispositions  aligned with parsed_rows: "new" or "matches:<transaction id>", a forecast (the
                      merge matches again inside its own transaction, so a sync in between is honoured)
    validation_result the §4.8 diagnostic; its unparseable_rows (with raw cells) are the third group

A row lives at most 24 hours: load() treats an expired row as gone even before
the daily cleanup deletes it, so nobody previews or merges stale parsed data.
"""

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from hisaab.models import Account, StatementStaging, utc_now
from hisaab.parsing.statement_interpreter import StatementRow
from hisaab.pipeline.derive import match_statement_rows
from hisaab.statements.reading import ACCOUNT_KIND, ReadStatement, StatementError

TTL: timedelta = timedelta(hours=24)
KIND_NAME: dict[str, str] = {"bank": "bank account", "credit_card": "credit card"}


def stage(session: Session, user_id: str, filename: str, content_hash: str, read: ReadStatement,
          now: datetime | None = None) -> StatementStaging:
    now = now or datetime.now(UTC)
    rows: list[StatementRow] = read.result.rows
    validation: dict[str, Any] = read.result.validation
    row: StatementStaging = StatementStaging(
        user_id=user_id, account_id=read.account_id, filename=filename, content_hash=content_hash,
        template_id=read.template.id, parsed_rows=json.dumps([dataclasses.asdict(r) for r in rows]),
        row_dispositions=json.dumps(_dispositions(session, user_id, read.account_id, rows)),
        declared_opening=validation["declared"]["opening"], declared_closing=validation["declared"]["closing"],
        validation_result=json.dumps({**validation, "account_kind": read.template.spec.account_kind}),
        expires_at=utc_now(now + TTL))
    session.add(row)
    session.flush()
    return row


def load(session: Session, user_id: str, staging_id: str) -> StatementStaging | None:
    """The user's own, unexpired staging row, or None."""
    row: StatementStaging | None = session.get(StatementStaging, staging_id)
    if row is None or row.user_id != user_id or row.expires_at <= utc_now():
        return None
    return row


def rows_of(staging: StatementStaging) -> list[StatementRow]:
    return [StatementRow(**r) for r in json.loads(staging.parsed_rows)]


def account_kind(staging: StatementStaging) -> str:
    kind: str = json.loads(staging.validation_result)["account_kind"]
    return kind


def rebind(session: Session, user_id: str, staging_id: str, account_id: str) -> StatementStaging:
    """The user picks the statement's account (binding failed, §4.7); the forecast follows the account."""
    staging: StatementStaging | None = load(session, user_id, staging_id)
    if staging is None:
        raise StatementError("This preview has expired or was discarded. Upload the statement again.")
    account: Account | None = session.get(Account, account_id)
    if account is None or account.user_id != user_id:
        raise StatementError("No such account.")
    if account.archived_at is not None:
        raise StatementError(f"{account.display_name} is archived. Pick an active account.")
    wanted: str = account_kind(staging)
    if ACCOUNT_KIND.get(account.type) != wanted:
        raise StatementError(f"This is a {KIND_NAME[wanted]} statement. Pick a {KIND_NAME[wanted]}.")
    staging.account_id = account.id
    staging.row_dispositions = json.dumps(_dispositions(session, user_id, account.id, rows_of(staging)))
    return staging


def _dispositions(session: Session, user_id: str, account_id: str | None, rows: list[StatementRow]) -> list[str]:
    if account_id is None:
        return ["new"] * len(rows)  # matching needs the account (equality is hard, §6.2)
    return [f"matches:{t}" if t else "new" for t in match_statement_rows(session, user_id, account_id, rows)]
