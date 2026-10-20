"""Template storage: save-time validation, deterministic candidate order, and the failure counter."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from hisaab.db import Database
from hisaab.models import ParserTemplate, Setting
from hisaab.parsing import templates
from hisaab.parsing.email_interpreter import EmailArtifact, EmailTemplate, Interpretation, interpret
from hisaab.parsing.email_spec import InvalidSpec
from tests.test_email_interpreter import BODY
from tests.test_email_spec import SPEC

NOW: datetime = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)



def save(db: Database, name: str, spec: dict[str, Any] = SPEC, **kw: Any) -> str:
    kw.setdefault("status", "active")
    with db.write_session() as s:
        return templates.save_email_template(s, name=name, spec=spec, **kw).id


def candidates(
    db: Database, sender: str = "alerts@example-bank.in", subject: str = "Debit alert",
) -> list[EmailTemplate]:
    with db.read_session() as s:
        return templates.email_candidates(s, sender, subject)


def counter(db: Database, template_id: str) -> tuple[int, str | None]:
    with db.read_session() as s:
        row: ParserTemplate = s.get_one(ParserTemplate, template_id)
        return row.consecutive_failures, row.failure_window_started_at


def broken(spec: dict[str, Any] = SPEC) -> dict[str, Any]:
    spec = copy.deepcopy(spec)
    spec["fields"]["amount"]["pattern"] = r"INR ([\d,.]+) spent"
    return spec


# --- saving -----------------------------------------------------------------------------

def test_an_invalid_spec_is_refused_and_nothing_is_stored(db: Database) -> None:
    bad: dict[str, Any] = copy.deepcopy(SPEC)
    bad["fields"]["amount"]["pattern"] = "(?=lookahead)(x)"
    with pytest.raises(InvalidSpec, match="amount"):
        save(db, "Bad", bad)
    with db.read_session() as s:
        assert s.scalar(select(func.count()).select_from(ParserTemplate)) == 0


def test_saving_copies_sender_and_subject_into_their_columns(db: Database) -> None:
    tid: str = save(db, "Example debit", status="proposed")
    with db.read_session() as s:
        row: ParserTemplate = s.get_one(ParserTemplate, tid)
        assert (row.artifact_type, row.sender_address, row.subject_pattern) == ("email", "alerts@example-bank.in", "^Debit alert")
        assert (row.origin, row.status, row.priority, row.consecutive_failures) == ("human", "proposed", 0, 0)
        assert json.loads(row.spec) == SPEC


# --- candidate selection (§4.3 step 1) ---------------------------------------------------

def test_only_active_email_templates_for_this_sender_and_subject_are_candidates(db: Database) -> None:
    keep: str = save(db, "Active")
    save(db, "Proposed", status="proposed")
    save(db, "Demoted", status="demoted")
    other_sender: dict[str, Any] = copy.deepcopy(SPEC)
    other_sender["match"]["sender"] = "alerts@other-bank.in"
    save(db, "Other sender", other_sender)
    other_subject: dict[str, Any] = copy.deepcopy(SPEC)
    other_subject["match"]["subject"] = "^Credit alert"
    save(db, "Other subject", other_subject)
    assert [c.id for c in candidates(db, sender="ALERTS@Example-Bank.in")] == [keep]
    assert candidates(db, subject="Your statement is ready") == []


def test_candidates_are_ordered_by_priority_then_most_recent_promotion_then_id(db: Database) -> None:
    low: str = save(db, "Low", priority=0, promoted_at="2026-10-09T00:00:00.000Z")
    never_promoted: str = save(db, "High, seed", priority=5, promoted_at=None)
    high_old: str = save(db, "High, old", priority=5, promoted_at="2026-09-01T00:00:00.000Z")
    # five exact ties: only the id tiebreak orders them (insertion order would match by luck 1 in 120)
    twins: list[str] = sorted(save(db, f"High, new {i}", priority=5, promoted_at="2026-10-01T00:00:00.000Z") for i in range(5))
    assert [c.id for c in candidates(db)] == [*twins, high_old, never_promoted, low]
    assert [c.id for c in candidates(db)] == [c.id for c in candidates(db)]  # same answer every time


def test_a_template_listing_several_senders_is_a_candidate_for_each(db: Database) -> None:
    moved: dict[str, Any] = copy.deepcopy(SPEC)
    moved["match"]["sender"] = ["alerts@example-bank.in", "alerts@example.bank.in"]
    tid: str = save(db, "Moved domain", moved)
    assert [c.id for c in candidates(db, sender="alerts@example.bank.in")] == [tid]
    assert [c.id for c in candidates(db, sender="Alerts@Example-Bank.in")] == [tid]
    assert candidates(db, sender="alerts@example.com") == []
    with db.read_session() as s:
        assert s.get_one(ParserTemplate, tid).sender_address == "alerts@example-bank.in"  # the first, for display


# --- the failure counter (§4.3 step 9, ruling 2026-10-11) --------------------------------

def run(db: Database, now: datetime = NOW) -> Interpretation:
    artifact: EmailArtifact = EmailArtifact("alerts@example-bank.in", "Debit alert", BODY, NOW)
    with db.write_session() as s:
        result: Interpretation = interpret(templates.email_candidates(s, artifact.sender, artifact.subject), artifact)
        templates.record_outcome(s, result, now=now)
    return result


def test_a_match_resets_the_winner_and_leaves_templates_that_declined_alone(db: Database) -> None:
    stale: str = save(db, "Stale", broken(), priority=1)
    good: str = save(db, "Good")
    with db.write_session() as s:
        for tid in (stale, good):
            row: ParserTemplate = s.get_one(ParserTemplate, tid)
            row.consecutive_failures, row.failure_window_started_at = 2, "2026-10-10T00:00:00.000Z"
    assert run(db).template_id == good
    assert counter(db, good) == (0, None)
    assert counter(db, stale) == (2, "2026-10-10T00:00:00.000Z")  # declining while a sibling matched is not a failure


def test_falling_through_charges_every_template_tried(db: Database) -> None:
    a: str = save(db, "A", broken(), priority=1)
    b: str = save(db, "B", broken())
    assert run(db).extraction is None
    assert counter(db, a) == (1, "2026-10-11T12:00:00.000Z")
    assert counter(db, b) == (1, "2026-10-11T12:00:00.000Z")
    run(db, now=NOW + timedelta(days=29))
    assert counter(db, a) == (2, "2026-10-11T12:00:00.000Z")  # the streak's window starts at its first failure


def test_a_failure_after_the_30_day_window_starts_a_new_streak(db: Database) -> None:
    a: str = save(db, "A", broken())
    run(db)
    run(db, now=NOW + timedelta(days=10))
    later: datetime = NOW + timedelta(days=31)
    run(db, now=later)
    assert counter(db, a) == (1, "2026-11-11T12:00:00.000Z")


# --- automatic demotion (§4.10, tunable template_demotion) ---------------------------------

def status(db: Database, template_id: str) -> tuple[str, str | None]:
    with db.read_session() as s:
        row: ParserTemplate = s.get_one(ParserTemplate, template_id)
        return row.status, row.demoted_at


def test_three_failures_within_30_days_demote_the_template(db: Database) -> None:
    a: str = save(db, "A", broken())
    run(db)
    run(db, now=NOW + timedelta(days=10))
    assert status(db, a) == ("active", None)
    run(db, now=NOW + timedelta(days=20))
    assert status(db, a) == ("demoted", "2026-10-31T12:00:00.000Z")
    assert candidates(db) == []  # a demoted template no longer reads anything


def test_a_match_between_failures_restarts_the_count(db: Database) -> None:
    a: str = save(db, "A", broken())
    run(db)
    run(db)
    with db.write_session() as s:  # one email it read: a match resets the streak (tested above)
        row: ParserTemplate = s.get_one(ParserTemplate, a)
        row.consecutive_failures, row.failure_window_started_at = 0, None
    run(db)
    run(db)
    assert status(db, a)[0] == "active"


def test_three_failures_spread_over_more_than_30_days_do_not_demote(db: Database) -> None:
    a: str = save(db, "A", broken())
    run(db)
    run(db, now=NOW + timedelta(days=20))
    run(db, now=NOW + timedelta(days=31))
    assert status(db, a)[0] == "active" and counter(db, a)[0] == 1


def test_the_threshold_is_a_tunable(db: Database) -> None:
    a: str = save(db, "A", broken())
    with db.write_session() as s:
        s.get_one(Setting, ("template_demotion", "")).value = json.dumps({"consecutive_failures": 2,
                                                                         "window_days": 30})
    run(db)
    run(db)
    assert status(db, a)[0] == "demoted"


# --- statement templates (§4.7 step 2) -----------------------------------------------------

from hisaab.parsing.pdf import Page, read_pdf
from hisaab.parsing.statement_interpreter import StatementTemplate, pick_statement_template
from hisaab.parsing.statement_spec import InvalidStatementSpec  # noqa: E402
from tests.pdf_factory import make_pdf  # noqa: E402
from tests.statement_samples import card_page, ruled_page  # noqa: E402
from tests.test_statement_spec import LINES, RULED  # noqa: E402
from typing import Any


def save_statement(db: Database, name: str, spec: dict[str, Any], **kw: Any) -> str:
    kw.setdefault("status", "active")
    with db.write_session() as s:
        return templates.save_statement_template(s, name=name, spec=spec, **kw).id


def statement_candidates(db: Database) -> list[StatementTemplate]:
    with db.read_session() as s:
        return templates.statement_candidates(s)


def test_an_invalid_statement_spec_is_refused(db: Database) -> None:
    bad: dict[str, Any] = copy.deepcopy(RULED)
    bad["account_kind"] = "wallet"
    with pytest.raises(InvalidStatementSpec):
        save_statement(db, "Bad", bad)
    assert statement_candidates(db) == []


def test_statement_templates_are_stored_without_email_columns(db: Database) -> None:
    tid: str = save_statement(db, "Example savings", RULED)
    with db.read_session() as s:
        row: ParserTemplate = s.get_one(ParserTemplate, tid)
        assert (row.artifact_type, row.sender_address, row.subject_pattern) == ("statement", None, None)


def test_the_first_active_template_whose_detect_matches_wins(db: Database) -> None:
    save_statement(db, "Proposed card", LINES, status="proposed")
    savings: str = save_statement(db, "Example savings", RULED)
    card: str = save_statement(db, "Example card", LINES, priority=1)
    candidates: list[StatementTemplate] = statement_candidates(db)
    assert [c.id for c in candidates] == [card, savings]  # priority first; the proposed one is not a candidate
    bank_pdf: list[Page] = read_pdf(make_pdf([ruled_page([], first=True)]))
    card_pdf: list[Page] = read_pdf(make_pdf([card_page([], first=True)]))
    picks: list[StatementTemplate | None] = [pick_statement_template(candidates, p) for p in (bank_pdf, card_pdf)]
    assert [t.id if t else None for t in picks] == [savings, card]
    assert pick_statement_template(candidates, read_pdf(make_pdf([{"texts": [(40, 40, "Unknown")]}]))) is None


def test_email_and_statement_candidates_do_not_mix(db: Database) -> None:
    save_statement(db, "Example savings", RULED)
    assert candidates(db) == []  # email candidates
