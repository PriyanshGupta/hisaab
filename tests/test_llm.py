"""Tier-2 email extraction (tech-design §4.1, §4.11; PRD §10.4) against a fake Claude."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr

from hisaab.config import Settings
from hisaab.llm import ContractError, LLMUnavailable, extract_email, validate_contract
from hisaab.parsing.email_interpreter import EmailArtifact
from tests.claude_fake import TRANSACTION, FakeClaude

ARTIFACT: EmailArtifact = EmailArtifact(
    sender="alerts@bank.example", subject="Debit alert",
    body="<html><body><p>Rs. 450.00 debited from a/c XX1234 to Corner Bakery</p></body></html>",
    received_at=datetime(2026, 10, 10, 3, 45, tzinfo=UTC),
)


@pytest.fixture
def llm_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"anthropic_api_key": SecretStr("sk-test")})


def extract(settings: Settings, fake: FakeClaude) -> dict[str, Any]:
    return extract_email(settings, ARTIFACT, transport=fake, sleep=lambda s: None)


def test_the_request_forces_the_contract_tool_on_the_configured_model(llm_settings: Settings) -> None:
    fake: FakeClaude = FakeClaude()
    assert extract(llm_settings, fake) == TRANSACTION
    request: dict[str, Any] = fake.requests[0]
    assert request["model"] == "claude-haiku-5-5"
    assert request["_headers"]["x-api-key"] == "sk-test" and "anthropic-version" in request["_headers"]
    assert request["tool_choice"] == {"type": "tool", "name": request["tools"][0]["name"]}
    prompt: str = request["messages"][0]["content"]
    assert "Rs. 450.00 debited" in prompt and "<p>" not in prompt  # html stripped to text
    assert "alerts@bank.example" in prompt and "Debit alert" in prompt
    assert "2026-10-10" in prompt  # the received date in India time, the fallback for a body with no date


def test_model_and_base_url_are_configuration(llm_settings: Settings) -> None:
    fake: FakeClaude = FakeClaude()
    urls: list[str] = []

    def transport(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, bytes]:
        urls.append(url)
        return fake(method, url, body, headers)

    configured: Settings = llm_settings.model_copy(update={"llm_base_url": "http://localhost:11434",
                                                           "email_model": "local-model"})
    extract_email(configured, ARTIFACT, transport=transport, sleep=lambda s: None)
    assert urls == ["http://localhost:11434/v1/messages"] and fake.requests[0]["model"] == "local-model"


def test_overload_is_retried_then_unavailable(llm_settings: Settings) -> None:
    fake: FakeClaude = FakeClaude(fail_next=[529, 503])
    assert extract(llm_settings, fake) == TRANSACTION
    fake = FakeClaude(fail_next=[529] * 4)
    with pytest.raises(LLMUnavailable, match="529"):
        extract(llm_settings, fake)
    assert fake.calls == 4


def test_no_api_key_means_unavailable_not_failed(settings: Settings) -> None:
    fake: FakeClaude = FakeClaude()
    with pytest.raises(LLMUnavailable, match="HISAAB_ANTHROPIC_API_KEY"):
        extract(settings, fake)
    assert fake.calls == 0


def test_a_reply_without_the_tool_call_is_a_contract_error(llm_settings: Settings) -> None:
    fake: FakeClaude = FakeClaude(raw_reply={"content": [{"type": "text", "text": "I think it is 450 rupees"}]})
    with pytest.raises(ContractError, match="tool"):
        extract(llm_settings, fake)


@pytest.mark.parametrize(("change", "problem"), [
    ({"amount": 450.0}, "amount"),
    ({"amount": "45000"}, "amount"),
    ({"amount": 0}, "amount"),
    ({"date": "10/10/2026"}, "date"),
    ({"time": "9:15am"}, "time"),
    ({"direction": "out"}, "direction"),
    ({"outcome": "maybe"}, "outcome"),
    ({"missing_fields": "amount"}, "missing_fields"),
    ({"amount": None}, "amount"),  # a transaction must carry an amount
])
def test_contract_violations_are_refused(change: dict[str, Any], problem: str) -> None:
    with pytest.raises(ContractError, match=problem):
        validate_contract({**TRANSACTION, **change})


def test_a_non_transaction_may_leave_everything_null() -> None:
    assert validate_contract({"outcome": "non_transactional"})["amount"] is None


def test_missing_keys_come_back_as_null() -> None:
    raw: dict[str, Any] = {k: v for k, v in TRANSACTION.items() if k not in ("time", "balance", "currency")}
    contract: dict[str, Any] = validate_contract(raw)
    assert (contract["time"], contract["balance"], contract["currency"]) == (None, None, None)
