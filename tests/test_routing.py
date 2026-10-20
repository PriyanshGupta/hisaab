"""Tier routing (tech-design §4.1): cache first, then tier 1 templates, then the LLM."""

import json
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import Connection

from hisaab.db import Database
from hisaab.llm import ContractError, LLMUnavailable
from hisaab.models import ExtractionCache, Source
from hisaab.parsing.email_interpreter import EmailArtifact
from hisaab.pipeline.routing import Routed, route
from tests import factories as f
from tests.claude_fake import MARKETING, TRANSACTION
from tests.pipeline_helpers import DEBIT_EML, NOTICE_EML, UNKNOWN_EML, add_source, install_templates

MODEL: str = "claude-haiku-5-5"


class Llm:
    """A stand-in for llm.extract_email: returns `reply`, or raises it if it is an exception."""

    def __init__(self, reply: dict[str, Any] | Exception = TRANSACTION) -> None:
        self.reply: dict[str, Any] | Exception = reply
        self.calls: int = 0

    def __call__(self, artifact: EmailArtifact) -> dict[str, Any]:
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture
def user_id(db: Database, conn: Connection) -> str:
    uid: str = f.user(conn)["id"]
    conn.commit()
    install_templates(db)
    return uid


@pytest.fixture
def routed(db: Database, user_id: str) -> Callable[[bytes, Llm], Routed]:
    def run(content: bytes, llm: Llm) -> Routed:
        with db.write_session() as s:
            source: Source = add_source(s, user_id, content, f"m-{len(content)}")
        with db.read_session() as s:
            return route(s, s.get_one(Source, source.id), llm, MODEL)
    return run


def test_a_known_format_parses_at_tier_1_and_its_content_goes(routed: Callable[[bytes, Llm], Routed]) -> None:
    llm: Llm = Llm()
    result: Routed = routed(DEBIT_EML, llm)
    assert (result.state, result.discard_content, llm.calls, result.cache_entry) == ("parsed", True, 0, None)
    assert result.extraction is not None and result.extraction["amount"] == 123450
    assert result.diagnostic["route"] == "tier1" and result.diagnostic["template"] == "Example debit"
    assert result.interpretation is not None  # for the template failure counters


def test_a_known_notice_is_non_transactional_at_tier_1(routed: Callable[[bytes, Llm], Routed]) -> None:
    result: Routed = routed(NOTICE_EML, Llm())
    assert (result.state, result.discard_content) == ("non_transactional", True)


def test_an_unknown_format_goes_to_the_llm_keeps_its_content_and_is_cached(
    routed: Callable[[bytes, Llm], Routed],
) -> None:
    llm: Llm = Llm()
    result: Routed = routed(UNKNOWN_EML, llm)
    assert (result.state, result.discard_content, llm.calls) == ("parsed", False, 1)
    assert result.cache_entry is not None
    assert (result.cache_entry.outcome, result.cache_entry.model, result.cache_entry.tier) == ("transaction", MODEL, 2)
    assert json.loads(result.cache_entry.extracted or "{}") == TRANSACTION
    assert result.fingerprint is not None
    assert result.diagnostic == {"route": "tier2", "model": MODEL, "outcome": "transaction", "missing_fields": [],
                                 "tier1_attempts": [], "sender": "offers@shop.example",
                                 "fingerprint": result.fingerprint.value}


def test_templates_that_declined_are_named_in_the_tier_2_diagnostic(routed: Callable[[bytes, Llm], Routed]) -> None:
    from tests.gmail_fake import make_eml
    changed: bytes = make_eml("alerts@example-bank.in", "Debit alert", "<p>A redesigned alert with no amount</p>")
    result: Routed = routed(changed, Llm())
    attempts: list[dict[str, Any]] = result.diagnostic["tier1_attempts"]
    assert [a["template"] for a in attempts] == ["Example debit"]
    assert any(failure.startswith("amount:") for failure in attempts[0]["failed"])
    assert "redesigned" not in json.dumps(result.diagnostic)  # never the email's own text


def test_a_cached_extraction_wins_before_any_template_or_model(db: Database, routed: Callable[[bytes, Llm], Routed]) -> None:
    import hashlib
    with db.write_session() as s:
        s.add(ExtractionCache(content_hash=hashlib.sha256(DEBIT_EML).hexdigest(), outcome="transaction",
                              extracted=json.dumps(TRANSACTION), model=MODEL, tier=2))
    llm: Llm = Llm()
    result: Routed = routed(DEBIT_EML, llm)
    assert (llm.calls, result.extraction, result.diagnostic["route"], result.interpretation) == (0, TRANSACTION, "cache", None)
    assert result.discard_content is False  # a tier-2 result: its format awaits promotion (§3.7)


def test_marketing_is_non_transactional_and_keeps_its_content(routed: Callable[[bytes, Llm], Routed]) -> None:
    result: Routed = routed(UNKNOWN_EML, Llm(MARKETING))
    assert (result.state, result.discard_content) == ("non_transactional", False)


def test_unparseable_is_failed_for_review(routed: Callable[[bytes, Llm], Routed]) -> None:
    result: Routed = routed(UNKNOWN_EML, Llm({**MARKETING, "outcome": "unparseable"}))
    assert (result.state, result.discard_content) == ("failed", False)


def test_an_unreachable_llm_leaves_it_pending_with_nothing_cached(routed: Callable[[bytes, Llm], Routed]) -> None:
    result: Routed = routed(UNKNOWN_EML, Llm(LLMUnavailable("LLM unavailable: HTTP 529 after 3 retries")))
    assert (result.state, result.discard_content, result.cache_entry, result.extraction) == ("pending_llm", False, None, None)
    assert "529" in result.diagnostic["reason"]


def test_an_off_contract_reply_fails_without_recording_the_bad_value(routed: Callable[[bytes, Llm], Routed]) -> None:
    result: Routed = routed(UNKNOWN_EML, Llm(ContractError("amount: '45000' is not a positive integer of paise")))
    assert (result.state, result.cache_entry) == ("failed", None)
    assert result.diagnostic["contract_violation"] == "amount" and "45000" not in json.dumps(result.diagnostic)


@pytest.mark.parametrize("content,llm", [(DEBIT_EML, Llm()), (UNKNOWN_EML, Llm()),
                                         (UNKNOWN_EML, Llm(LLMUnavailable("down")))],
                         ids=["tier1", "tier2", "pending_llm"])
def test_every_diagnostic_names_the_sender_for_dedup(routed: Callable[[bytes, Llm], Routed], content: bytes,
                                                     llm: Llm) -> None:
    sender: str = "alerts@example-bank.in" if content == DEBIT_EML else "offers@shop.example"
    assert routed(content, llm).diagnostic["sender"] == sender


def test_a_cache_hit_names_the_sender_too(db: Database, routed: Callable[[bytes, Llm], Routed]) -> None:
    import hashlib
    with db.write_session() as s:
        s.add(ExtractionCache(content_hash=hashlib.sha256(UNKNOWN_EML).hexdigest(), outcome="transaction",
                              extracted=json.dumps(TRANSACTION), model=MODEL, tier=2))
    assert routed(UNKNOWN_EML, Llm()).diagnostic["sender"] == "offers@shop.example"
