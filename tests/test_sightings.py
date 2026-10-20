"""Sightings (tech-design §4.10 steps 1-2): unknown formats counted by fingerprint at tier 2.

Transactional sightings become the examples (capped at the promotion threshold); two non-transactional
sightings exclude the format, after which its emails skip the model (ruling 2026-10-10).
"""

import hashlib
import json
from typing import Any

import pytest
from sqlalchemy import Connection, delete, select, update

from hisaab.config import Settings
from hisaab.db import Database
from hisaab.jobs.queue import JobContext
from hisaab.llm import LLMUnavailable
from hisaab.models import ExtractionCache, FormatSighting, ParserTemplate, Source
from hisaab.parsing.email_interpreter import parse_eml
from hisaab.parsing.fingerprint import fingerprint
from hisaab.pipeline.process import process_pending
from hisaab.pipeline.routing import Routed, route
from hisaab.promotion.sightings import examples, sight_retained
from tests import factories as f
from tests.pipeline_helpers import add_source
from tests.test_email_spec import SPEC
from tests.promotion_helpers import (
    MARKETING_READING,
    MODEL,
    Haiku,
    alert_eml,
    feed,
    offer_eml,
    reading,
    rerun,
    sources,
)


@pytest.fixture
def uid(conn: Connection) -> str:
    user_id: str = f.user(conn)["id"]
    conn.commit()
    return user_id


def sighting(db: Database) -> FormatSighting:
    with db.read_session() as s:
        row: FormatSighting = s.scalars(select(FormatSighting)).one()
        s.expunge(row)
        return row


def test_a_first_sighting_starts_accumulating(db: Database, uid: str) -> None:
    [sid] = feed(db, uid, [alert_eml(0)], Haiku(reading(0)))
    row: FormatSighting = sighting(db)
    assert (row.state, row.sighting_count, examples(row)) == ("accumulating", 1, [sid])
    assert row.fingerprint == fingerprint(parse_eml(alert_eml(0))).value
    assert row.sender_address == "alerts@lantern.example" and row.subject_masked == "You have done a UPI txn"
    assert json.loads(row.structural_markers)["static_lines"][0] == "has been debited from account"
    assert json.loads(sources(db)[sid].diagnostic or "{}")["fingerprint"] == row.fingerprint


def test_three_transactional_sightings_are_the_examples_and_a_fourth_only_counts(db: Database, uid: str) -> None:
    ids: list[str] = feed(db, uid, [alert_eml(n) for n in range(4)], Haiku(*[reading(n) for n in range(4)]))
    row: FormatSighting = sighting(db)
    assert (row.state, row.sighting_count, examples(row)) == ("accumulating", 4, ids[:3])
    assert all(sources(db)[i].content is not None for i in ids)  # tier 2 keeps content until promotion


def test_two_non_transactional_sightings_exclude_the_format_and_drop_its_content(db: Database, uid: str) -> None:
    ids: list[str] = feed(db, uid, [offer_eml(0), offer_eml(1)], Haiku(MARKETING_READING, MARKETING_READING))
    row: FormatSighting = sighting(db)
    assert (row.state, row.sighting_count, examples(row)) == ("excluded_non_transactional", 2, [])
    assert [sources(db)[i].content for i in ids] == [None, None]


def test_an_excluded_format_skips_the_model(db: Database, uid: str) -> None:
    feed(db, uid, [offer_eml(0), offer_eml(1)], Haiku(MARKETING_READING, MARKETING_READING))
    no_model: Haiku = Haiku()  # raises StopIteration if asked
    [sid] = feed(db, uid, [offer_eml(2)], no_model)
    src: Source = sources(db)[sid]
    assert no_model.calls == 0 and src.processing_state == "non_transactional" and src.content is None
    assert json.loads(src.diagnostic or "{}")["route"] == "excluded"
    assert sighting(db).sighting_count == 3


def test_one_non_transactional_among_transactions_changes_nothing(db: Database, uid: str) -> None:
    ids: list[str] = feed(db, uid, [alert_eml(0), alert_eml(1)], Haiku(reading(0), MARKETING_READING))
    row: FormatSighting = sighting(db)
    assert (row.state, row.sighting_count, examples(row)) == ("accumulating", 2, ids[:1])


def test_an_unparseable_reading_is_not_counted(db: Database, uid: str) -> None:
    feed(db, uid, [alert_eml(0)], Haiku({**MARKETING_READING, "outcome": "unparseable"}))
    with db.read_session() as s:
        assert s.scalars(select(FormatSighting)).all() == []


def test_a_model_outage_records_nothing(db: Database, uid: str) -> None:
    feed(db, uid, [alert_eml(0)], Haiku(LLMUnavailable("down")))
    with db.read_session() as s:
        assert s.scalars(select(FormatSighting)).all() == []


def test_reading_an_email_again_does_not_count_it_twice(db: Database, conn: Connection, uid: str) -> None:
    [sid] = feed(db, uid, [alert_eml(0)], Haiku(reading(0)))
    conn.execute(delete(ExtractionCache))  # force tier 2 again, as after a cache invalidation
    conn.commit()
    rerun(db, uid, [sid], Haiku(reading(0)))
    assert sighting(db).sighting_count == 1 and examples(sighting(db)) == [sid]


def test_a_promoted_format_whose_template_was_demoted_starts_over(db: Database, conn: Connection, uid: str) -> None:
    feed(db, uid, [alert_eml(0)], Haiku(reading(0)))
    template: str = f.parser_template(conn, status="demoted")["id"]
    conn.execute(update(FormatSighting).values(state="promoted", template_id=template))
    conn.commit()
    [sid] = feed(db, uid, [alert_eml(1)], Haiku(reading(1)))
    row: FormatSighting = sighting(db)
    assert (row.state, row.template_id, examples(row), row.sighting_count) == ("accumulating", None, [sid], 2)


def test_a_promoted_format_with_an_active_template_only_counts(db: Database, conn: Connection, uid: str) -> None:
    [first] = feed(db, uid, [alert_eml(0)], Haiku(reading(0)))
    template: str = f.parser_template(conn, status="active", spec=json.dumps(SPEC))["id"]  # another bank's
    conn.execute(update(FormatSighting).values(state="promoted", template_id=template))
    conn.commit()
    feed(db, uid, [alert_eml(1)], Haiku(reading(1)))
    row: FormatSighting = sighting(db)
    assert (row.state, row.template_id, examples(row), row.sighting_count) == ("promoted", template, [first], 2)


def test_routing_puts_the_fingerprint_on_tier_2_only(db: Database, uid: str) -> None:
    with db.write_session() as s:
        sid: str = add_source(s, uid, alert_eml(0), "m-1").id
    with db.read_session() as s:
        result: Routed = route(s, s.get_one(Source, sid), Haiku(reading(0)), MODEL)
    assert result.fingerprint is not None and result.diagnostic["fingerprint"] == result.fingerprint.value


# --- emails read before this slice: counted from their cached reading, no model call ---------------------------

def retained(db: Database, conn: Connection, uid: str, eml: bytes, contract: dict[str, Any], seq_hint: str) -> str:
    digest: str = hashlib.sha256(eml).hexdigest()
    f.extraction_cache(conn, content_hash=digest, outcome=contract["outcome"], extracted=json.dumps(contract),
                       model=MODEL, tier=2)
    conn.commit()
    state: str = "parsed" if contract["outcome"] == "transaction" else "non_transactional"
    with db.write_session() as s:
        return add_source(s, uid, eml, seq_hint, processing_state=state,
                          diagnostic=json.dumps({"route": "tier2", "outcome": contract["outcome"]})).id


def test_retained_emails_from_before_are_counted_once(db: Database, conn: Connection, uid: str) -> None:
    ids: list[str] = [retained(db, conn, uid, alert_eml(n), reading(n), f"old-{n}") for n in range(3)]
    offers: list[str] = [retained(db, conn, uid, offer_eml(n), MARKETING_READING, f"offer-{n}") for n in range(2)]
    with db.write_session() as s:
        assert sight_retained(s, uid) == 5
    with db.write_session() as s:
        assert sight_retained(s, uid) == 0
    with db.read_session() as s:
        rows: dict[str, FormatSighting] = {r.state: r for r in s.scalars(select(FormatSighting))}
        s.expunge_all()
    assert examples(rows["accumulating"]) == ids and rows["accumulating"].sighting_count == 3
    assert rows["excluded_non_transactional"].sighting_count == 2
    assert [sources(db)[i].content for i in offers] == [None, None]


def test_a_retained_email_read_by_a_template_is_not_counted(db: Database, conn: Connection, uid: str) -> None:
    with db.write_session() as s:
        add_source(s, uid, alert_eml(0), "t1", processing_state="parsed", diagnostic=json.dumps({"route": "tier1"}))
    with db.write_session() as s:
        assert sight_retained(s, uid) == 0
    with db.read_session() as s:
        assert s.scalars(select(ParserTemplate)).all() == [] and s.scalars(select(FormatSighting)).all() == []


def test_a_sync_run_counts_retained_emails_before_reading_new_ones(db: Database, conn: Connection, uid: str,
                                                                   settings: Settings) -> None:
    ids: list[str] = [retained(db, conn, uid, alert_eml(n), reading(n), f"old-{n}") for n in range(2)]
    process_pending(JobContext(db, "job", uid, {}), uid, settings, lambda *a: (0, b""), lambda s: None)
    assert examples(sighting(db)) == ids
