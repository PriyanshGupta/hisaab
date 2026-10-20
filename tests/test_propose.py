"""Template proposal (tech-design §4.10 steps 2-3): at three transactional sightings Sonnet proposes a spec,
replay validates it, and it waits for the admin. The model is a fake; every email is invented."""

import copy
import json
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Connection, select, update

from hisaab.config import Settings
from hisaab.db import Database
from hisaab.jobs.queue import JobContext
from hisaab.models import FormatSighting, ParserTemplate, Source
from hisaab.promotion.propose import build_spec, propose_ready
from hisaab.promotion.sightings import examples
from tests import factories as f
from tests.claude_fake import FakeClaude
from tests.promotion_helpers import LANTERN, Haiku, alert_eml, feed, reading, sources


def field(kind: str, pattern: str = "", transform: str = "", value: str = "",
          required: bool = True) -> dict[str, Any]:
    return {"kind": kind, "pattern": pattern, "transform": transform, "value": value, "required": required}


ANSWER: dict[str, Any] = {
    "name": "lantern_upi_debit",
    "subject_pattern": "^You have done a UPI txn",
    "fields": {
        "amount": field("pattern", r"Rs\.([\d,]+\.\d{2}) has been debited", "inr_to_paise"),
        "currency": field("const", value="INR"),
        "direction": field("const", value="debit"),
        "account_identifier": field("pattern", r"from account \*\*(\d{4})"),
        "counterparty_identifier": field("pattern", r"to VPA (\S+)"),
        "payee_raw": field("pattern", r"to VPA \S+ (.+?) on \d"),
        "reference_number": field("pattern", r"reference number is (\d+)"),
        "date": field("pattern", r" on (\d{2}-\d{2}-\d{2})\.", "date:%d-%m-%y"),
        "time": field("absent", required=False),
        "balance": field("absent", required=False),
    },
}
WRONG: dict[str, Any] = copy.deepcopy(ANSWER)
WRONG["fields"]["reference_number"] = field("pattern", r"reference number is (\d{6})")


@pytest.fixture
def uid(conn: Connection) -> str:
    user_id: str = f.user(conn)["id"]
    conn.commit()
    return user_id


@pytest.fixture
def llm_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"anthropic_api_key": SecretStr("test-key")})


def propose(db: Database, uid: str, settings: Settings, fake: FakeClaude) -> dict[str, int]:
    return propose_ready(JobContext(db, "job", uid, {}), settings, fake, lambda s: None)


def three_seen(db: Database, uid: str) -> list[str]:
    return feed(db, uid, [alert_eml(n) for n in range(3)], Haiku(*[reading(n) for n in range(3)]))


def state(db: Database) -> tuple[FormatSighting, list[ParserTemplate]]:
    with db.read_session() as s:
        row: FormatSighting = s.scalars(select(FormatSighting)).one()
        templates: list[ParserTemplate] = list(s.scalars(select(ParserTemplate)))
        s.expunge_all()
        return row, templates


def test_three_sightings_bring_one_proposal_that_passes_replay(db: Database, uid: str, llm_settings: Settings) -> None:
    ids: list[str] = three_seen(db, uid)
    fake: FakeClaude = FakeClaude(reply=lambda prompt: ANSWER)
    assert propose(db, uid, llm_settings, fake) == {"proposed": 1, "validated": 1, "failed": 0, "auto_promoted": 0}
    [request] = fake.requests
    assert request["model"] == llm_settings.template_model == "claude-sonnet-5-5"
    prompt: str = request["messages"][0]["content"]
    assert all(name in prompt for name in ("Corner Grocer", "RAVI KUMAR", "Lamp House", '"reference_number": "655544433322"'))
    row, [template] = state(db)
    assert (row.state, row.template_id) == ("proposed", template.id)
    assert (template.status, template.origin, template.name) == ("validated", "llm_proposed", "lantern_upi_debit")
    assert json.loads(template.example_source_ids or "[]") == ids
    assert json.loads(template.validation_result or "{}")["passed"] is True
    spec: dict[str, Any] = json.loads(template.spec)
    assert spec["match"] == {"sender": LANTERN, "subject": "^You have done a UPI txn"}
    assert spec["preprocess"] == ["strip_html", "collapse_whitespace"]


def test_a_candidate_that_fails_replay_stays_visible_and_the_sighting_keeps_accumulating(
        db: Database, uid: str, llm_settings: Settings) -> None:
    three_seen(db, uid)
    fake: FakeClaude = FakeClaude(reply=lambda prompt: WRONG)
    assert propose(db, uid, llm_settings, fake)["failed"] == 1
    row, [template] = state(db)
    assert (row.state, row.template_id, template.status) == ("accumulating", template.id, "proposed")
    result: dict[str, Any] = json.loads(template.validation_result or "{}")
    assert result["passed"] is False and "email 1: reference_number differs" in result["problems"]
    propose(db, uid, llm_settings, fake)
    assert fake.calls == 1  # the same three examples are never sent twice


def test_a_new_example_after_a_failure_replaces_the_oldest_and_asks_again(db: Database, uid: str,
                                                                          llm_settings: Settings) -> None:
    ids: list[str] = three_seen(db, uid)
    propose(db, uid, llm_settings, FakeClaude(reply=lambda prompt: WRONG))
    [fourth] = feed(db, uid, [alert_eml(3)], Haiku(reading(3)))
    row, _ = state(db)
    assert examples(row) == [*ids[1:], fourth] and sources(db)[ids[0]].content is None
    fake: FakeClaude = FakeClaude(reply=lambda prompt: ANSWER)
    assert propose(db, uid, llm_settings, fake)["validated"] == 1
    row, [template] = state(db)  # the failed candidate is gone
    assert (row.state, template.status, json.loads(template.example_source_ids or "[]")) == \
        ("proposed", "validated", [*ids[1:], fourth])


def test_sonnet_unreachable_writes_nothing_and_the_next_run_asks(db: Database, uid: str,
                                                                 llm_settings: Settings) -> None:
    three_seen(db, uid)
    down: FakeClaude = FakeClaude(reply=lambda prompt: ANSWER, fail_next=[503] * 4)
    assert propose(db, uid, llm_settings, down) == {"proposed": 0, "validated": 0, "failed": 0, "auto_promoted": 0}
    row, templates = state(db)
    assert (row.state, row.template_id, templates) == ("accumulating", None, [])
    assert propose(db, uid, llm_settings, FakeClaude(reply=lambda prompt: ANSWER))["validated"] == 1


def test_no_key_configured_asks_nothing(db: Database, uid: str, settings: Settings) -> None:
    three_seen(db, uid)
    fake: FakeClaude = FakeClaude(reply=lambda prompt: ANSWER)
    assert propose(db, uid, settings, fake)["proposed"] == 0 and fake.calls == 0


def test_an_example_without_its_content_is_never_sent_short(db: Database, conn: Connection, uid: str,
                                                            llm_settings: Settings) -> None:
    ids: list[str] = three_seen(db, uid)
    conn.execute(update(Source).where(Source.id == ids[1]).values(content=None))
    conn.commit()
    fake: FakeClaude = FakeClaude(reply=lambda prompt: ANSWER)
    assert propose(db, uid, llm_settings, fake)["proposed"] == 0 and fake.calls == 0


def test_a_name_already_taken_gets_a_suffix(db: Database, conn: Connection, uid: str, llm_settings: Settings) -> None:
    f.parser_template(conn, name="lantern_upi_debit", status="demoted")
    conn.commit()
    three_seen(db, uid)
    propose(db, uid, llm_settings, FakeClaude(reply=lambda prompt: ANSWER))
    with db.read_session() as s:
        assert s.scalar(select(ParserTemplate.name).where(ParserTemplate.origin == "llm_proposed")) == "lantern_upi_debit_2"


def test_an_unusable_answer_is_a_failed_candidate_not_a_retry_loop(db: Database, uid: str,
                                                                   llm_settings: Settings) -> None:
    three_seen(db, uid)
    fake: FakeClaude = FakeClaude(reply=lambda prompt: {"name": "half", "subject_pattern": "^You"})
    assert propose(db, uid, llm_settings, fake)["failed"] == 1
    _, [template] = state(db)
    assert template.status == "proposed" and json.loads(template.validation_result or "{}")["passed"] is False
    propose(db, uid, llm_settings, fake)
    assert fake.calls == 1


def test_build_spec_turns_the_form_into_a_spec() -> None:
    spec: dict[str, Any] = build_spec(ANSWER, LANTERN)
    assert spec["fields"]["currency"] == {"const": "INR"}
    assert spec["fields"]["time"] == {"required": False}
    assert spec["fields"]["amount"] == {"pattern": r"Rs\.([\d,]+\.\d{2}) has been debited", "transform": "inr_to_paise"}
    assert spec["fields"]["counterparty_identifier"] == {"pattern": r"to VPA (\S+)"}


def test_a_proposal_that_cannot_be_made_says_why_in_the_log(db: Database, uid: str, llm_settings: Settings,
                                                             caplog: pytest.LogCaptureFixture) -> None:
    three_seen(db, uid)
    refused: FakeClaude = FakeClaude(raw_replies=[])
    refused.fail_next = [400]
    with caplog.at_level("WARNING", logger="hisaab.promotion.propose"):
        assert propose(db, uid, llm_settings, refused)["proposed"] == 0
    assert "HTTP 400" in caplog.text and "You have done a UPI txn" in caplog.text  # which format waits, and why


def test_a_candidate_failed_by_validation_alone_is_rechecked_without_asking_again(
        db: Database, conn: Connection, uid: str, llm_settings: Settings) -> None:
    three_seen(db, uid)
    propose(db, uid, llm_settings, FakeClaude(reply=lambda prompt: ANSWER))
    with db.read_session() as s:
        template_id: str = s.scalars(select(ParserTemplate.id)).one()
    # as stored by an older validation that wrongly failed it
    conn.execute(update(ParserTemplate).values(status="proposed", validation_result=json.dumps(
        {"passed": False, "problems": ["email 1: time differs"], "examples": [{"source_id": "x", "matched": True,
                                                                               "fields": []}]})))
    conn.execute(update(FormatSighting).values(state="accumulating"))
    conn.commit()
    fake: FakeClaude = FakeClaude(reply=lambda prompt: ANSWER)
    assert propose(db, uid, llm_settings, fake)["validated"] == 1 and fake.calls == 0
    row, [template] = state(db)
    assert (template.id, template.status, row.state) == (template_id, "validated", "proposed")


def test_a_rejected_candidate_is_never_revived(db: Database, uid: str, llm_settings: Settings) -> None:
    from hisaab.promotion.promote import reject
    three_seen(db, uid)
    propose(db, uid, llm_settings, FakeClaude(reply=lambda prompt: ANSWER))
    with db.write_session() as s:
        reject(s, s.scalars(select(ParserTemplate.id)).one())
    fake: FakeClaude = FakeClaude(reply=lambda prompt: ANSWER)
    assert propose(db, uid, llm_settings, fake)["validated"] == 0 and fake.calls == 0
    _, [template] = state(db)
    assert template.status == "proposed"
