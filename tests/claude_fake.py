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


# Models that refuse forced tool use (tool_choice "tool"/"any") with a 400 (claude-api reference, 2026-10).
NO_FORCED_TOOL_CHOICE: frozenset[str] = frozenset({"claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1",
                                                   "claude-mythos-5-1"})


def _strict_problems(schema: Any, where: str) -> list[str]:
    """Strict tool use needs additionalProperties: false and every property required, on every object."""
    if not isinstance(schema, dict):
        return []
    found: list[str] = []
    if schema.get("type") == "object" or "properties" in schema:
        if schema.get("additionalProperties") is not False:
            found.append(f"{where}: additionalProperties must be false for strict tools")
        if set(schema.get("properties", {})) != set(schema.get("required", [])):
            found.append(f"{where}: every property must be required for strict tools")
    for key, sub in schema.get("properties", {}).items():
        found += _strict_problems(sub, f"{where}.{key}")
    if "items" in schema:
        found += _strict_problems(schema["items"], f"{where}[]")
    return found


def request_problem(request: dict[str, Any]) -> str | None:
    """The first thing about a request the real Messages API would reject, as far as these tests need."""
    choice: dict[str, Any] = request.get("tool_choice", {})
    if request.get("model") in NO_FORCED_TOOL_CHOICE and choice.get("type") in ("tool", "any"):
        return 'tool_choice: type "tool" and "any" are not supported for this model.'
    for tool in request.get("tools", []):
        if tool.get("strict"):
            problems: list[str] = _strict_problems(tool.get("input_schema"), tool.get("name", "tool"))
            if problems:
                return problems[0]
    return None


@dataclass
class FakeClaude:
    reply: Callable[[str], dict[str, Any]] = lambda prompt: TRANSACTION  # prompt text -> tool input
    fail_next: list[int] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    raw_reply: dict[str, Any] | None = None  # a whole response body, to test malformed replies
    raw_replies: list[dict[str, Any]] = field(default_factory=list)  # whole bodies, one per request, first

    @property
    def calls(self) -> int:
        return len(self.requests)

    def __call__(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, bytes]:
        assert (method, url.rsplit("/", 2)[-2:]) == ("POST", ["v1", "messages"]), url
        request: dict[str, Any] = json.loads(body or b"{}")
        request["_headers"] = headers
        self.requests.append(request)
        problem: str | None = request_problem(request)
        if problem is not None:  # what the real API refuses with a 400
            return 400, json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": problem}}).encode()
        if self.fail_next:
            return self.fail_next.pop(0), b'{"type": "error"}'
        if self.raw_replies:
            return 200, json.dumps(self.raw_replies.pop(0)).encode()
        if self.raw_reply is not None:
            return 200, json.dumps(self.raw_reply).encode()
        prompt: str = request["messages"][0]["content"]
        # forced: the named tool; auto: the request's own (non-server) tool, as a model finishing its answer would
        tool: str = request["tool_choice"].get("name") or next(t["name"] for t in request["tools"] if "input_schema" in t)
        return 200, json.dumps({
            "type": "message", "role": "assistant", "model": request["model"], "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": tool, "input": self.reply(prompt)}],
        }).encode()
