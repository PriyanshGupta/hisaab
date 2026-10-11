"""Template storage: save-time validation, deterministic candidate order, and the failure counter."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from hisaab.db import Database
from hisaab.models import ParserTemplate
from hisaab.parsing import templates
from hisaab.parsing.email_interpreter import EmailArtifact, interpret
from hisaab.parsing.email_spec import InvalidSpec
from tests.test_email_interpreter import BODY
from tests.test_email_spec import SPEC

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(migrated_engine):
    return Database(migrated_engine)


def save(db, name, spec=SPEC, **kw):
    kw.setdefault("status", "active")
    with db.write_session() as s:
        return templates.save_email_template(s, name=name, spec=spec, **kw).id


def candidates(db, sender="alerts@example-bank.in", subject="Debit alert"):
    with db.read_session() as s:
        return templates.email_candidates(s, sender, subject)


def counter(db, template_id):
    with db.read_session() as s:
        row = s.get(ParserTemplate, template_id)
        return row.consecutive_failures, row.failure_window_started_at


def broken(spec=SPEC):
    spec = copy.deepcopy(spec)
    spec["fields"]["amount"]["pattern"] = r"INR ([\d,.]+) spent"
    return spec


# --- saving -----------------------------------------------------------------------------

def test_an_invalid_spec_is_refused_and_nothing_is_stored(db):
    bad = copy.deepcopy(SPEC)
    bad["fields"]["amount"]["pattern"] = "(?=lookahead)(x)"
    with pytest.raises(InvalidSpec, match="amount"):
        save(db, "Bad", bad)
    with db.read_session() as s:
        assert s.scalar(select(func.count()).select_from(ParserTemplate)) == 0


def test_saving_copies_sender_and_subject_into_their_columns(db):
    tid = save(db, "Example debit", status="proposed")
    with db.read_session() as s:
        row = s.get(ParserTemplate, tid)
        assert (row.artifact_type, row.sender_address, row.subject_pattern) == ("email", "alerts@example-bank.in", "^Debit alert")
        assert (row.origin, row.status, row.priority, row.consecutive_failures) == ("human", "proposed", 0, 0)
        assert json.loads(row.spec) == SPEC


# --- candidate selection (§4.3 step 1) ---------------------------------------------------

def test_only_active_email_templates_for_this_sender_and_subject_are_candidates(db):
    keep = save(db, "Active")
    save(db, "Proposed", status="proposed")
    save(db, "Demoted", status="demoted")
    other_sender = copy.deepcopy(SPEC)
    other_sender["match"]["sender"] = "alerts@other-bank.in"
    save(db, "Other sender", other_sender)
    other_subject = copy.deepcopy(SPEC)
    other_subject["match"]["subject"] = "^Credit alert"
    save(db, "Other subject", other_subject)
    assert [c.id for c in candidates(db, sender="ALERTS@Example-Bank.in")] == [keep]
    assert candidates(db, subject="Your statement is ready") == []


def test_candidates_are_ordered_by_priority_then_most_recent_promotion_then_id(db):
    low = save(db, "Low", priority=0, promoted_at="2026-10-09T00:00:00.000Z")
    never_promoted = save(db, "High, seed", priority=5, promoted_at=None)
    high_old = save(db, "High, old", priority=5, promoted_at="2026-09-01T00:00:00.000Z")
    # five exact ties: only the id tiebreak orders them (insertion order would match by luck 1 in 120)
    twins = sorted(save(db, f"High, new {i}", priority=5, promoted_at="2026-10-01T00:00:00.000Z") for i in range(5))
    assert [c.id for c in candidates(db)] == [*twins, high_old, never_promoted, low]
    assert [c.id for c in candidates(db)] == [c.id for c in candidates(db)]  # same answer every time


def test_a_template_listing_several_senders_is_a_candidate_for_each(db):
    moved = copy.deepcopy(SPEC)
    moved["match"]["sender"] = ["alerts@example-bank.in", "alerts@example.bank.in"]
    tid = save(db, "Moved domain", moved)
    assert [c.id for c in candidates(db, sender="alerts@example.bank.in")] == [tid]
    assert [c.id for c in candidates(db, sender="Alerts@Example-Bank.in")] == [tid]
    assert candidates(db, sender="alerts@example.com") == []
    with db.read_session() as s:
        assert s.get(ParserTemplate, tid).sender_address == "alerts@example-bank.in"  # the first, for display


# --- the failure counter (§4.3 step 9, ruling 2026-10-11) --------------------------------

def run(db, now=NOW):
    artifact = EmailArtifact("alerts@example-bank.in", "Debit alert", BODY, NOW)
    with db.write_session() as s:
        result = interpret(templates.email_candidates(s, artifact.sender, artifact.subject), artifact)
        templates.record_outcome(s, result, now=now)
    return result


def test_a_match_resets_the_winner_and_leaves_templates_that_declined_alone(db):
    stale = save(db, "Stale", broken(), priority=1)
    good = save(db, "Good")
    with db.write_session() as s:
        for tid in (stale, good):
            row = s.get(ParserTemplate, tid)
            row.consecutive_failures, row.failure_window_started_at = 2, "2026-10-10T00:00:00.000Z"
    assert run(db).template_id == good
    assert counter(db, good) == (0, None)
    assert counter(db, stale) == (2, "2026-10-10T00:00:00.000Z")  # declining while a sibling matched is not a failure


def test_falling_through_charges_every_template_tried(db):
    a = save(db, "A", broken(), priority=1)
    b = save(db, "B", broken())
    assert run(db).extraction is None
    assert counter(db, a) == (1, "2026-10-11T12:00:00.000Z")
    assert counter(db, b) == (1, "2026-10-11T12:00:00.000Z")
    run(db, now=NOW + timedelta(days=29))
    assert counter(db, a) == (2, "2026-10-11T12:00:00.000Z")  # the streak's window starts at its first failure


def test_a_failure_after_the_30_day_window_starts_a_new_streak(db):
    a = save(db, "A", broken())
    run(db)
    run(db, now=NOW + timedelta(days=10))
    later = NOW + timedelta(days=31)
    run(db, now=later)
    assert counter(db, a) == (1, "2026-11-11T12:00:00.000Z")
