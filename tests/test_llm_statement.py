"""Tier-3 statement extraction (tech-design §4.11, PRD §10.4): the request, and the answer validated row by row."""

import copy
from typing import Any

import pytest
from pydantic import SecretStr

from hisaab.config import Settings
from hisaab.llm import ContractError, LLMUnavailable, extract_email, extract_statement, validate_statement
from tests.claude_fake import FakeClaude
from tests.test_llm import ARTIFACT

ROW: dict[str, Any] = {"page": 1, "date": "2026-09-01", "time": None, "narration": "UPI-TEA STALL", "reference": None,
                       "direction": "debit", "amount": 12300, "balance": 87700, "bill_payment": False}
STATEMENT: dict[str, Any] = {
    "outcome": "statement", "account_kind": "bank", "account_last4": "1234", "institution": "Example Bank",
    "opening": 100000, "closing": 83200,
    "rows": [ROW, {**ROW, "page": 2, "date": "2026-09-02", "narration": "BUS", "amount": 4500, "balance": 83200}],
    "unreadable": [],
}


def no_sleep(_s: float) -> None:
    pass


@pytest.fixture
def llm_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"anthropic_api_key": SecretStr("sk-test")})


def answer(**changes: Any) -> dict[str, Any]:
    return copy.deepcopy(STATEMENT) | changes


def test_pages_go_to_the_statement_model_through_the_forced_tool(llm_settings: Settings) -> None:
    claude: FakeClaude = FakeClaude(reply=lambda prompt: answer())
    got: dict[str, Any] = extract_statement(llm_settings, ["page one text", "page two text"], transport=claude,
                                            sleep=no_sleep)
    request: dict[str, Any] = claude.requests[0]
    assert request["model"] == "claude-sonnet-5-5"
    # Sonnet 5.5 refuses forced tool use (400): auto plus a strict tool and the prompt's instruction instead
    assert request["tool_choice"] == {"type": "auto"} and request["tools"][0]["strict"] is True
    assert request["output_config"] == {"effort": "medium"}
    assert "=== Page 1 ===\npage one text" in request["messages"][0]["content"]
    assert "=== Page 2 ===\npage two text" in request["messages"][0]["content"]
    assert [r["amount"] for r in got["rows"]] == [12300, 4500] and got["unreadable"] == []
    assert (got["account_kind"], got["account_last4"], got["opening"], got["closing"]) == ("bank", "1234", 100000, 83200)


def test_the_statement_model_is_configuration(llm_settings: Settings) -> None:
    claude: FakeClaude = FakeClaude(reply=lambda prompt: answer())
    extract_statement(llm_settings.model_copy(update={"statement_model": "local-model"}), ["p"], transport=claude,
                      sleep=no_sleep)
    assert claude.requests[0]["model"] == "local-model"


def test_email_calls_still_use_the_email_model(llm_settings: Settings) -> None:
    claude: FakeClaude = FakeClaude()
    extract_email(llm_settings, ARTIFACT, transport=claude, sleep=no_sleep)
    assert claude.requests[0]["model"] == llm_settings.email_model


@pytest.mark.parametrize(("change", "reason"), [
    ({"date": "2026-02-30"}, "date"),
    ({"page": 9}, "page"),
    ({"amount": 12.5}, "amount"),
    ({"amount": 0}, "amount"),
    ({"direction": "sideways"}, "direction"),
    ({"narration": None}, "narration"),
])
def test_a_malformed_row_is_unreadable_not_a_failure(change: dict[str, Any], reason: str) -> None:
    raw: dict[str, Any] = answer(rows=[ROW, {**ROW, **change}])
    got: dict[str, Any] = validate_statement(raw, page_count=2)
    assert [r["amount"] for r in got["rows"]] == [12300]
    [bad] = got["unreadable"]
    assert reason in bad["reason"] and bad["page"] in (1, 9)


def test_unreadable_lines_from_the_model_are_kept() -> None:
    got: dict[str, Any] = validate_statement(
        answer(unreadable=[{"page": 2, "text": "14/08 NEFT ... 2,000.00", "reason": "two amounts"}]), page_count=2)
    assert got["unreadable"] == [{"page": 2, "text": "14/08 NEFT ... 2,000.00", "reason": "two amounts"}]


@pytest.mark.parametrize("change", [
    {"outcome": "maybe"}, {"rows": "lots"}, {"account_last4": "12"}, {"opening": "1,000.00"},
    {"account_kind": "wallet"}, {"unreadable": "none"},
])
def test_a_malformed_envelope_is_refused_whole(change: dict[str, Any]) -> None:
    with pytest.raises(ContractError):
        validate_statement(answer(**change), page_count=2)


def test_not_a_statement_carries_no_rows() -> None:
    assert validate_statement(answer(outcome="not_a_statement", rows=[]), page_count=1)["rows"] == []
    with pytest.raises(ContractError):
        validate_statement(answer(outcome="not_a_statement"), page_count=2)


def test_a_cut_off_answer_is_refused(llm_settings: Settings) -> None:
    claude: FakeClaude = FakeClaude(raw_reply={"type": "message", "stop_reason": "max_tokens", "content": []})
    with pytest.raises(ContractError, match="cut off"):
        extract_statement(llm_settings, ["p"], transport=claude, sleep=no_sleep)


def test_no_retry_on_failure(llm_settings: Settings) -> None:
    claude: FakeClaude = FakeClaude(fail_next=[529, 529])
    with pytest.raises(LLMUnavailable):
        extract_statement(llm_settings, ["p"], transport=claude, sleep=no_sleep)
    assert claude.calls == 1  # a read can take ten minutes; a retry would double the wait (the person re-uploads)


def test_the_request_meets_sonnet_5_5_rules(llm_settings: Settings) -> None:
    from tests.claude_fake import request_problem

    claude: FakeClaude = FakeClaude(reply=lambda prompt: answer())
    extract_statement(llm_settings, ["p"], transport=claude, sleep=no_sleep)
    assert request_problem(claude.requests[0]) is None


def test_an_answer_without_the_tool_is_asked_once_more(llm_settings: Settings) -> None:
    text_only: dict[str, Any] = {"type": "message", "stop_reason": "end_turn", "content": [{"type": "text", "text": "Here"}]}
    tool: dict[str, Any] = {"type": "message", "stop_reason": "tool_use", "content": [
        {"type": "tool_use", "id": "t", "name": "record_statement", "input": answer()}]}
    claude: FakeClaude = FakeClaude(raw_replies=[text_only, tool])
    assert len(extract_statement(llm_settings, ["p", "q"], transport=claude, sleep=no_sleep)["rows"]) == 2
    assert claude.calls == 2  # tool_choice auto doesn't guarantee the call: one more ask, then refuse


def test_a_dropped_connection_mid_answer_is_unavailable_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    import http.client
    import urllib.request

    from hisaab.llm import long_transport

    def dropped(*args: Any, **kwargs: Any) -> Any:
        raise http.client.IncompleteRead(b"partial")
    monkeypatch.setattr(urllib.request, "urlopen", dropped)
    assert long_transport(5)("POST", "https://example.invalid/v1/messages", b"{}", {}) == (0, b"")
