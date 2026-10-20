"""Slice 22 whole-branch review: each finding reproduced first, then fixed. Every email is invented."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Connection, func, select, update

from hisaab.config import Settings
from hisaab.db import Database
from hisaab.jobs.queue import JobContext
from hisaab.models import ExtractionCache, FormatSighting, ParserTemplate, Source
from hisaab.promotion.promote import ApproveResult, approve
from hisaab.promotion.propose import propose_ready
from tests import factories as f
from tests.claude_fake import FakeClaude
from tests.promotion_helpers import Haiku, alert_eml, feed, reading, rerun, sources
from tests.test_propose import ANSWER


@pytest.fixture
def uid(conn: Connection) -> str:
    user_id: str = f.user(conn, display_name="Asha Rao")["id"]
    conn.commit()
    return user_id


@pytest.fixture
def cfg(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(update={"anthropic_api_key": SecretStr("test-key"), "fixtures_dir": tmp_path})


def sighting(db: Database) -> FormatSighting:
    with db.read_session() as s:
        row: FormatSighting = s.scalars(select(FormatSighting)).one()
        s.expunge(row)
        return row


def propose_and_approve(db: Database, uid: str, cfg: Settings) -> ApproveResult:
    propose_ready(JobContext(db, "job", uid, {}), cfg, FakeClaude(reply=lambda prompt: ANSWER), lambda s: None)
    with db.write_session() as s:
        template_id: str = s.scalars(select(ParserTemplate.id).where(ParserTemplate.origin == "llm_proposed")).one()
        return approve(s, template_id, cfg)


# --- #1: a re-sync (cache hit) must keep the fingerprint ----------------------------------------------------

def test_a_resynced_email_keeps_its_fingerprint_is_not_recounted_and_is_reread_on_approval(
        db: Database, conn: Connection, uid: str, cfg: Settings) -> None:
    ids: list[str] = feed(db, uid, [alert_eml(n) for n in range(4)], Haiku(*[reading(n) for n in range(4)]))
    conn.execute(update(Source).where(Source.id == ids[3]).values(processing_state="pending"))  # re-sync requeue
    conn.commit()
    rerun(db, uid, [ids[3]], Haiku())  # the cache answers; no model call
    diagnostic: dict[str, Any] = json.loads(sources(db)[ids[3]].diagnostic or "{}")
    assert diagnostic["route"] == "cache" and diagnostic["fingerprint"] == sighting(db).fingerprint
    assert sighting(db).sighting_count == 4
    result: ApproveResult = propose_and_approve(db, uid, cfg)
    assert (result.invalidated, result.reread) == (4, 4)
    with db.read_session() as s:
        assert s.scalar(select(func.count()).select_from(ExtractionCache)) == 0
    assert json.loads(sources(db)[ids[3]].diagnostic or "{}")["route"] == "tier1"


# --- #2: a format with transactional readings is never excluded; an admin can bring one back ------------------

from tests.promotion_helpers import MARKETING_READING  # noqa: E402


def test_two_misreadings_never_exclude_a_format_that_has_transactions(db: Database, uid: str) -> None:
    feed(db, uid, [alert_eml(n) for n in range(4)],
         Haiku(reading(0), MARKETING_READING, reading(2), MARKETING_READING))
    assert sighting(db).state == "accumulating"


def test_an_excluded_format_can_be_read_again(db: Database, uid: str) -> None:
    from hisaab.promotion.sightings import include_again
    from tests.promotion_helpers import offer_eml
    feed(db, uid, [offer_eml(0), offer_eml(1)], Haiku(MARKETING_READING, MARKETING_READING))
    assert sighting(db).state == "excluded_non_transactional"
    with db.write_session() as s:
        include_again(s, sighting(db).id)
    assert sighting(db).state == "accumulating"
    model: Haiku = Haiku(MARKETING_READING)
    feed(db, uid, [offer_eml(2)], model)
    assert model.calls == 1  # asked again, not short-circuited


# --- #3: approval's own re-reads never count as the new template's failures -----------------------------------

def test_emails_the_new_template_declines_on_approval_do_not_demote_it(db: Database, uid: str, cfg: Settings) -> None:
    from tests.gmail_fake import make_eml
    from tests.promotion_helpers import ALERT, LANTERN, PEOPLE
    feed(db, uid, [alert_eml(n) for n in range(3)], Haiku(*[reading(n) for n in range(3)]))
    whole: list[bytes] = [make_eml(LANTERN, "You have done a UPI txn",
                                   ALERT.format(verb="debited", **{**PEOPLE[3], "amount": str(300 + n), "ref": f"6{n}0000000001"}))
                          for n in range(3)]
    feed(db, uid, whole, Haiku(*[reading(3)] * 3))
    result: ApproveResult = propose_and_approve(db, uid, cfg)
    assert (result.reread, result.waiting) == (3, 3)
    with db.read_session() as s:
        template: ParserTemplate = s.scalars(select(ParserTemplate).where(ParserTemplate.origin == "llm_proposed")).one()
        assert (template.status, template.consecutive_failures) == ("active", 0)


# --- #4: measure-fixtures reads exported fixtures with the template they were exported for -------------------

def test_exported_fixtures_measure_as_correct_with_their_exported_template(db: Database, uid: str, cfg: Settings,
                                                                           tmp_path: Path) -> None:
    from hisaab.parsing.measure import Summary, Templates, load_promoted_templates, measure, summarize
    feed(db, uid, [alert_eml(n) for n in range(3)], Haiku(*[reading(n) for n in range(3)]))
    propose_and_approve(db, uid, cfg)
    assert (tmp_path / "promoted" / "lantern_upi_debit.template.json").is_file()
    templates: Templates = Templates(load_promoted_templates(tmp_path), [])
    summary: Summary = summarize(measure(tmp_path, templates, frozenset()))
    assert (summary.transactions, summary.correct) == (3, 3)


# --- #5: redaction leaks and the amount it corrupted --------------------------------------------------------

def test_redaction_catches_salutation_names_ids_link_addresses_and_leaves_amounts_alone() -> None:
    from hisaab.parsing.email_interpreter import parse_eml
    from hisaab.promotion.redact import redact
    from tests.gmail_fake import make_eml
    from tests.promotion_helpers import LANTERN
    html: str = ("<html><body><p>Dear Asha KAPOOR,</p>"
                 "<p>Rs.4321.00 has been debited from account **4321 on 05-10-26.</p>"
                 "<p>Customer ID 87654321.</p>"
                 '<p><a href="https://lantern.example/u?u=asha.kapoor%40mail.example">Unsubscribe</a></p>'
                 "</body></html>")
    contract: dict[str, Any] = {**reading(0), "amount": 432100, "account_identifier": "4321"}
    out, expected = redact(make_eml(LANTERN, "Alert", html), contract, ["Asha Rao"])
    text: str = out.decode() + parse_eml(out).body
    for secret in ("KAPOOR", "Kapoor", "87654321", "kapoor%40", "asha.kapoor"):
        assert secret not in text, secret
    assert "Rs.4321.00 has been debited" in parse_eml(out).body  # the amount, untouched
    assert expected["amount"] == 432100 and expected["account_identifier"] != "4321"
    assert "Unsubscribe" in parse_eml(out).body and "05-10-26" in parse_eml(out).body


# --- #7: counting retained emails is per user ----------------------------------------------------------------

def test_one_users_sync_counts_only_that_users_retained_emails(db: Database, conn: Connection, uid: str) -> None:
    import hashlib
    from hisaab.promotion.sightings import sight_retained
    from tests.pipeline_helpers import add_source
    other: str = f.user(conn)["id"]
    conn.commit()
    for owner, n in ((uid, 0), (other, 1)):
        eml: bytes = alert_eml(n)
        f.extraction_cache(conn, content_hash=hashlib.sha256(eml).hexdigest(), extracted=json.dumps(reading(n)))
        conn.commit()
        with db.write_session() as s:
            add_source(s, owner, eml, f"old-{n}", processing_state="parsed", diagnostic=json.dumps({"route": "tier2"}))
    with db.write_session() as s:
        assert sight_retained(s, uid) == 1
    with db.read_session() as s:
        theirs: Source = s.scalars(select(Source).where(Source.user_id == other)).one()
        assert "fingerprint" not in json.loads(theirs.diagnostic or "{}")


# --- #13: Review Focus 3 and 4, as the plan promised -----------------------------------------------------------

def test_approve_exports_only_the_examples_it_still_has(db: Database, conn: Connection, uid: str, cfg: Settings,
                                                        tmp_path: Path) -> None:
    ids: list[str] = feed(db, uid, [alert_eml(n) for n in range(3)], Haiku(*[reading(n) for n in range(3)]))
    propose_ready(JobContext(db, "job", uid, {}), cfg, FakeClaude(reply=lambda prompt: ANSWER), lambda s: None)
    conn.execute(update(Source).where(Source.id == ids[1]).values(content=None))  # lost after the proposal
    conn.commit()
    with db.write_session() as s:
        template_id: str = s.scalars(select(ParserTemplate.id).where(ParserTemplate.origin == "llm_proposed")).one()
        result: ApproveResult = approve(s, template_id, cfg)
    assert result.exported == ["promoted/lantern_upi_debit_01.eml", "promoted/lantern_upi_debit_02.eml"]


def test_examples_can_come_from_two_users_and_each_is_reread_in_its_own_pipeline(
        db: Database, conn: Connection, uid: str, cfg: Settings) -> None:
    other: str = f.user(conn, display_name="Vikram Shah")["id"]
    conn.commit()
    mine: list[str] = feed(db, uid, [alert_eml(0), alert_eml(1)], Haiku(reading(0), reading(1)))
    [theirs] = feed(db, other, [alert_eml(2)], Haiku(reading(2)))
    from hisaab.promotion.sightings import examples
    assert examples(sighting(db)) == [*mine, theirs]
    result: ApproveResult = propose_and_approve(db, uid, cfg)
    assert (result.reread, len(result.exported)) == (3, 3)
    with db.read_session() as s:
        owners: dict[str, str] = {sid: s.get_one(Source, sid).user_id for sid in [*mine, theirs]}
    assert owners[theirs] == other and json.loads(sources(db)[theirs].diagnostic or "{}")["route"] == "tier1"
    exported: str = (cfg.fixtures_dir / "promoted" / "lantern_upi_debit_03.eml").read_text()
    assert "Vikram" not in exported and "Asha" not in exported


def test_the_bare_digits_of_a_masked_account_are_redacted_too() -> None:
    """Found in live verification: a bank's 'type BLOCK CC 4321' instruction repeats the card's last digits."""
    from hisaab.parsing.email_interpreter import parse_eml
    from hisaab.promotion.redact import redact
    from tests.gmail_fake import make_eml
    from tests.promotion_helpers import LANTERN
    html: str = ("<p>Thank you for using your card XX4321 for Rs. 512.00 on 05-10-2026.</p>"
                 "<p>To block it, type BLOCK CC 4321 and send it to us.</p>")
    out, expected = redact(make_eml(LANTERN, "Alert", html), {**reading(0), "account_identifier": "XX4321"}, [])
    body: str = parse_eml(out).body
    assert "4321" not in body and "4321" not in out.decode()
    assert "Rs. 512.00" in body and expected["account_identifier"] in body
