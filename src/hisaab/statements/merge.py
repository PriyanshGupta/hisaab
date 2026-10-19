"""The statement_merge job (tech-design §7 step 6): one write transaction, all or nothing.

    refuse    the preview expired or isn't the user's; no active account; the check failed and the
              user didn't choose "Merge anyway" (the handler checks too; this is the authority)
    Source    kind statement, content NULL: the PDF was never stored (§3.7, ruling 2026-10-19);
              the diagnostic is kept, with accepted_gap_paise when the user merged a failed check
    rows      derive.apply_statement: match again, supersede, insert, pair, resolve status
    balance   the declared closing balance as a BalanceObservation, at the end of the last row's day
              (no movement after it, so that is when the closing figure is true)
    discard   the staging row is deleted

Reconciliation against the observation, and adjustments for an accepted gap, are slice 20.
"""

import json
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hisaab.clock import LOCAL_TZ
from hisaab.jobs.queue import JobContext
from hisaab.models import Account, BalanceObservation, Source, StatementStaging, utc_now
from hisaab.parsing.statement_interpreter import StatementRow
from hisaab.pipeline.derive import StatementCounts, apply_statement
from hisaab.statements.staging import load, rows_of
from hisaab.transactions.repository import next_ingest_seq


class MergeRefused(Exception):
    """The merge can't go ahead (message is a sentence for the person; the job fails with it)."""


def run(ctx: JobContext) -> dict[str, int]:
    assert ctx.user_id is not None  # enqueued by a user's request
    with ctx.write() as s:
        staging: StatementStaging | None = load(s, ctx.user_id, ctx.params["staging_id"])
        if staging is None:
            raise MergeRefused("This preview has expired or was discarded. Upload the statement again.")
        account: Account | None = s.get(Account, staging.account_id) if staging.account_id else None
        if account is None or account.user_id != ctx.user_id or account.archived_at is not None:
            raise MergeRefused("Pick an active account for this statement first.")
        diagnostic: dict[str, Any] = json.loads(staging.validation_result)
        if diagnostic["outcome"] == "failed":
            if not ctx.params.get("accept_gap"):
                raise MergeRefused("The rows don't add up to the statement's closing balance. "
                                   "Merge anyway to keep the gap on record, or discard the statement.")
            diagnostic["accepted_gap_paise"] = diagnostic["gap_paise"]
        now: str = utc_now()
        source: Source = Source(ingest_seq=next_ingest_seq(s), user_id=ctx.user_id, kind="statement", content=None,
                                content_hash=staging.content_hash, external_message_id=None, received_at=now,
                                ingested_at=now, processing_state="parsed",
                                diagnostic=json.dumps({**diagnostic, "filename": staging.filename}))
        s.add(source)
        s.flush()
        rows: list[StatementRow] = rows_of(staging)
        counts: StatementCounts = apply_statement(s, ctx.user_id, source, account.id, rows)
        if staging.declared_closing is not None and rows:
            _observe_closing(s, ctx.user_id, account.id, staging.declared_closing,
                             max(date.fromisoformat(r.date) for r in rows), source)
        s.delete(staging)
    return {"new": counts.new, "matched": counts.matched, "needs_review": counts.needs_review,
            "unparseable": int(diagnostic["rows_unparseable"])}


def _observe_closing(session: Session, user_id: str, account_id: str, balance: int, last: date,
                     source: Source) -> None:
    """One observation per account and statement date: uploading the statement again updates it."""
    observed_at: str = utc_now(datetime.combine(last, time(23, 59, 59), LOCAL_TZ))
    seen: BalanceObservation | None = session.scalars(select(BalanceObservation).where(
        BalanceObservation.user_id == user_id, BalanceObservation.account_id == account_id,
        BalanceObservation.source == "statement", BalanceObservation.observed_at == observed_at)).first()
    if seen is None:
        session.add(BalanceObservation(user_id=user_id, account_id=account_id, balance=balance,
                                       observed_at=observed_at, source="statement", source_id=source.id))
    else:
        seen.balance, seen.source_id = balance, source.id
