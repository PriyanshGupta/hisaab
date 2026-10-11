"""Parser templates (tech-design §3.3): the only writer of parser_templates.
Templates are GLOBAL: one bank format serves every user.

- save_email_template validates the spec before storing it (§4.5), so a
  template with an uncompilable, over-long or ill-typed pattern never exists.
- email_candidates is §4.3 step 1: active email templates listing this sender
  whose subject pattern matches, in a fully deterministic order. A spec may
  list several senders (ruling 2026-10-11), so sender_address holds only the
  first, for display; matching reads the spec.
- save_statement_template / statement_candidates are the statement side: active
  statement templates in the same deterministic order; the interpreter's
  pick_statement_template takes the first whose detect matches (§4.7 step 2).
- record_outcome is §4.3 step 9, the counter automatic demotion reads (§4.10;
  the demote itself is slice 16).
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ScalarResult, select
from sqlalchemy.orm import Session

from hisaab.models import ParserTemplate
from hisaab.models.base import utc_now
from hisaab.parsing import patterns
from hisaab.parsing.email_interpreter import EmailTemplate, Interpretation
from hisaab.parsing.email_spec import EmailSpec, parse_email_spec
from hisaab.parsing.statement_interpreter import StatementTemplate
from hisaab.parsing.statement_spec import parse_statement_spec

FAILURE_WINDOW: timedelta = timedelta(days=30)  # PRD §10.1: 3 consecutive failures within 30 days demotes


def save_email_template(
    session: Session, *, name: str, spec: dict[str, Any], origin: str = "human", status: str = "proposed",
    priority: int = 0, promoted_at: str | None = None,
) -> ParserTemplate:
    parsed: EmailSpec = parse_email_spec(spec)  # raises InvalidSpec listing every problem
    row: ParserTemplate = ParserTemplate(
        artifact_type="email", name=name, sender_address=parsed.senders[0], subject_pattern=spec["match"]["subject"],
        spec=json.dumps(spec, ensure_ascii=False), origin=origin, status=status, priority=priority,
        promoted_at=promoted_at,
    )
    session.add(row)
    session.flush()
    return row


def email_candidates(session: Session, sender: str, subject: str) -> list[EmailTemplate]:
    """Order: priority DESC, promoted_at DESC (§4.3), never-promoted seeds after promoted
    ones, then id, so even exact ties resolve the same way every run (PRD §8.4)."""
    # shortcut: loads every active email template and filters in Python; fine for dozens of
    # templates, add a sender lookup table if it ever reaches thousands.
    rows: ScalarResult[ParserTemplate] = session.scalars(
        select(ParserTemplate)
        .where(ParserTemplate.artifact_type == "email", ParserTemplate.status == "active")
        .order_by(ParserTemplate.priority.desc(), ParserTemplate.promoted_at.desc().nulls_last(), ParserTemplate.id)
    )
    ordered: list[EmailTemplate] = [EmailTemplate(id=row.id, name=row.name, spec=parse_email_spec(json.loads(row.spec)))
                                    for row in rows]
    return select_email_candidates(ordered, sender, subject)


def select_email_candidates(templates: list[EmailTemplate], sender: str, subject: str) -> list[EmailTemplate]:
    """The templates (kept in the order given) that list this sender and whose subject pattern matches."""
    sender = sender.strip().lower()
    found: list[EmailTemplate] = []
    for template in templates:
        spec: EmailSpec = template.spec
        if sender not in spec.senders:
            continue
        try:
            # SQLite has no RE2, so subjects filter here (a parsed spec always has one)
            if spec.subject is None or patterns.search(spec.subject, subject) is None:
                continue
        except patterns.PatternTimeout:
            continue
        found.append(template)
    return found


def save_statement_template(
    session: Session, *, name: str, spec: dict[str, Any], status: str = "proposed", priority: int = 0,
    promoted_at: str | None = None,
) -> ParserTemplate:
    parse_statement_spec(spec)  # raises InvalidStatementSpec listing every problem
    row: ParserTemplate = ParserTemplate(  # statement templates are human-authored only (tech-design §10 amendment 2)
        artifact_type="statement", name=name, spec=json.dumps(spec, ensure_ascii=False), origin="human",
        status=status, priority=priority, promoted_at=promoted_at,
    )
    session.add(row)
    session.flush()
    return row


def statement_candidates(session: Session) -> list[StatementTemplate]:
    rows: ScalarResult[ParserTemplate] = session.scalars(
        select(ParserTemplate)
        .where(ParserTemplate.artifact_type == "statement", ParserTemplate.status == "active")
        .order_by(ParserTemplate.priority.desc(), ParserTemplate.promoted_at.desc().nulls_last(), ParserTemplate.id)
    )
    return [StatementTemplate(row.id, row.name, parse_statement_spec(json.loads(row.spec))) for row in rows]


def record_outcome(session: Session, result: Interpretation, now: datetime | None = None) -> None:
    """Ruling 2026-10-11, amending §4.3 step 9. A match resets the winner's streak.
    Templates that declined while another matched are left alone: templates sharing
    a sender and subject decline each other's emails by design. Only when every
    candidate declined (fall-through to tier 2) is each one charged a failure.
    A streak runs from its first failure; one that comes after FAILURE_WINDOW
    starts a new streak at 1."""
    now = now or datetime.now(UTC)
    if result.extraction is not None:
        winner: ParserTemplate | None = session.get(ParserTemplate, result.template_id)
        if winner is not None:  # None only if the template was deleted mid-run
            winner.consecutive_failures, winner.failure_window_started_at = 0, None
        return
    stamp: str = utc_now(now)
    window_floor: str = utc_now(now - FAILURE_WINDOW)
    for attempt in result.attempts:
        row: ParserTemplate | None = session.get(ParserTemplate, attempt.template_id)
        if row is None:
            continue
        started: str | None = row.failure_window_started_at
        if row.consecutive_failures and started and started > window_floor:
            row.consecutive_failures += 1
        else:
            row.consecutive_failures, row.failure_window_started_at = 1, stamp
