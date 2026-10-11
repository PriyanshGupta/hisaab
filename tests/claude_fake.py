"""A fake Anthropic Messages API for the LLM, routing and sync tests.

Answers POST /v1/messages through the same injectable transport hisaab.llm
uses, replying with a forced tool call whose input is the next canned
extraction (or one chosen by a function of the prompt). Records every request.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

TRANSACTION: dict[str, Any] = {
    "outcome": "transaction", "amount": 45000, "currency": "INR", "direction": "debit",
    "account_identifier": "XX1234", "counterparty_identifier": None, "payee_raw": "Corner Bakery",
    "reference_number": "612345678901", "date": "2026-10-10", "time": "09:15", "balance": None, "missing_fields": [],
}
MARKETING: dict[str, Any] = {
    "outcome": "non_transactional", "amount": None, "currency": None, "direction": None, "account_identifier": None,
    "counterparty_identifier": None, "payee_raw": None, "reference_number": None, "date": None, "time": None,
    "balance": None, "missing_fields": [],
}


@dataclass
class FakeClaude:
    reply: Callable[[str], dict[str, Any]] = lambda prompt: TRANSACTION  # prompt text -> tool input
    fail_next: list[int] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    raw_reply: dict[str, Any] | None = None  # a whole response body, to test malformed replies

    @property
    def calls(self) -> int:
        return len(self.requests)

    def __call__(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, bytes]:
        assert (method, url.rsplit("/", 2)[-2:]) == ("POST", ["v1", "messages"]), url
        request: dict[str, Any] = json.loads(body or b"{}")
        request["_headers"] = headers
        self.requests.append(request)
        if self.fail_next:
            return self.fail_next.pop(0), b'{"type": "error"}'
        if self.raw_reply is not None:
            return 200, json.dumps(self.raw_reply).encode()
        prompt: str = request["messages"][0]["content"]
        tool: str = request["tool_choice"]["name"]
        return 200, json.dumps({
            "type": "message", "role": "assistant", "model": request["model"], "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": tool, "input": self.reply(prompt)}],
        }).encode()
