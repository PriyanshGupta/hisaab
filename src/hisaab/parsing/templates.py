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
- save_candidate stores an LLM-proposed template for the promotion queue (§4.10), valid or not.
- record_outcome is §4.3 step 9, and demotes a template whose failures reach the
  tunable template_demotion threshold (§4.10).
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
from hisaab.tunables import tunable



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


def save_candidate(session: Session, *, name: str, spec: dict[str, Any], example_source_ids: list[str],
                   validation: dict[str, Any]) -> ParserTemplate:
    """An LLM-proposed email template awaiting review (§4.10). Stored even when its spec is invalid or its
    replay failed, so the queue shows the discarded candidate (§4.9): status 'proposed', never approvable.
    Only a passing one is 'validated'. Neither status is ever a parsing candidate (only 'active' is)."""
    match: dict[str, Any] = spec.get("match") or {}
    sender: Any = match.get("sender")
    row: ParserTemplate = ParserTemplate(
        artifact_type="email", name=name, sender_address=sender if isinstance(sender, str) else None,
        subject_pattern=match.get("subject") if isinstance(match.get("subject"), str) else None,
        spec=json.dumps(spec, ensure_ascii=False), origin="llm_proposed",
        status="validated" if validation["passed"] else "proposed",
        example_source_ids=json.dumps(example_source_ids), validation_result=json.dumps(validation, ensure_ascii=False),
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
        # SQLite has no RE2, so subjects filter here (a parsed spec always has one)
        if spec.subject is None or not patterns.matches(spec.subject, subject):
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
    A streak runs from its first failure; one that comes after the window
    starts a new streak at 1. A streak reaching the threshold demotes the template
    (§4.10; tunable template_demotion, 3 within 30 days): it stops matching, and its
    emails go back to the model, where the format accumulates sightings again."""
    now = now or datetime.now(UTC)
    if result.extraction is not None:
        winner: ParserTemplate | None = session.get(ParserTemplate, result.template_id)
        if winner is not None:  # None only if the template was deleted mid-run
            winner.consecutive_failures, winner.failure_window_started_at = 0, None
        return
    rule: dict[str, int] = tunable(session, "template_demotion")
    stamp: str = utc_now(now)
    window_floor: str = utc_now(now - timedelta(days=rule["window_days"]))
    for attempt in result.attempts:
        row: ParserTemplate | None = session.get(ParserTemplate, attempt.template_id)
        if row is None:
            continue
        started: str | None = row.failure_window_started_at
        if row.consecutive_failures and started and started > window_floor:
            row.consecutive_failures += 1
        else:
            row.consecutive_failures, row.failure_window_started_at = 1, stamp
        if row.consecutive_failures >= rule["consecutive_failures"] and row.status == "active":
            row.status, row.demoted_at = "demoted", stamp
