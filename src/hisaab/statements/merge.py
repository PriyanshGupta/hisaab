"""The statement_merge job (tech-design §7 step 6): one write transaction, all or nothing.

    refuse    the preview expired or isn't the user's; no active account; the check failed and the
              user didn't choose "Merge anyway" (the handler checks too; this is the authority)
    Source    kind statement, content NULL: the PDF was never stored (§3.7, ruling 2026-10-19);
              the diagnostic is kept, with accepted_gap_paise when the user merged a failed check
    rows      derive.apply_statement: match again, supersede, insert, pair, resolve status
    balance   the declared closing balance as a BalanceObservation, at the end of the last row's day
              (no movement after it, so that is when the closing figure is true), and the declared
              opening at the end of the day before the first row, so the statement is checked against
              the ledger at once (slice 20, ruling 2026-10-20)
    discard   the staging row is deleted

    check     record_checks: any gap between those balances and the ledger opens a balance_deltas row
"""

import json
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hisaab.balances.checks import record_checks
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
        if rows:
            first: date = min(date.fromisoformat(r.date) for r in rows)
            last: date = max(date.fromisoformat(r.date) for r in rows)
            wanted: list[tuple[int, date]] = [
                (balance, on) for balance, on in ((staging.declared_opening, first - timedelta(days=1)),  # slice 20
                                                  (staging.declared_closing, last)) if balance is not None]
            _observe(s, ctx.user_id, account.id, wanted, source, staging.content_hash)
        s.delete(staging)
        gaps: int = record_checks(s, ctx.user_id)
    return {"new": counts.new, "matched": counts.matched, "needs_review": counts.needs_review,
            "unparseable": int(diagnostic["rows_unparseable"]), "balance_gaps": gaps}


def _observe(session: Session, user_id: str, account_id: str, wanted: list[tuple[int, date]], source: Source,
             content_hash: str) -> None:
    """The balances the statement prints (its opening, then its closing), each at the end of its date.

    Matched to the observations an earlier upload of the SAME statement (same content hash) recorded:
    the same instant first, then opening/closing order, so a re-upload updates them in place even when a
    parser fix moved a row date (their ids, and any gap recorded against them, stay). Another statement's
    figure at the same instant (August's closing, September's opening) is never overwritten: both are kept,
    and if the bank's two documents disagree the check shows the difference."""
    earlier: list[BalanceObservation] = list(session.scalars(
        select(BalanceObservation).join(Source, Source.id == BalanceObservation.source_id).where(
            BalanceObservation.user_id == user_id, BalanceObservation.account_id == account_id,
            BalanceObservation.source == "statement", Source.content_hash == content_hash)
        .order_by(BalanceObservation.observed_at)))
    instants: list[tuple[int, str]] = [(balance, utc_now(datetime.combine(on, time(23, 59, 59), LOCAL_TZ)))
                                       for balance, on in wanted]
    unmatched: list[BalanceObservation] = list(earlier)
    plan: list[tuple[int, str, BalanceObservation | None]] = []
    for balance, observed_at in instants:
        same: BalanceObservation | None = next((o for o in unmatched if o.observed_at == observed_at), None)
        if same is not None:
            unmatched.remove(same)
        plan.append((balance, observed_at, same))
    for balance, observed_at, same in plan:
        seen: BalanceObservation | None = same or (unmatched.pop(0) if unmatched else None)
        if seen is None:
            session.add(BalanceObservation(user_id=user_id, account_id=account_id, balance=balance,
                                           observed_at=observed_at, source="statement", source_id=source.id))
        else:
            seen.balance, seen.observed_at, seen.source_id = balance, observed_at, source.id
    session.flush()
