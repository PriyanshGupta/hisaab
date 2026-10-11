"""Tier-2 email extraction by an LLM (tech-design §4.1, §4.11) against the PRD §10.4 contract.

Speaks the Anthropic Messages API at a configurable base URL and model
(HISAAB_LLM_BASE_URL, HISAAB_EMAIL_MODEL), so pointing the email tier at a
local server that serves the same API needs no code change. The model must
answer through one forced tool call whose input schema is the contract; the
reply is then validated field by field, and anything off-contract is refused
(ContractError) rather than stored half-right.

LLMUnavailable (no key, network down, overloaded after retries) is not a parse
failure: the Source waits as pending_llm and is retried on the next sync.
"""

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import date
from typing import Any, TypeAlias

from hisaab.clock import LOCAL_TZ
from hisaab.config import Settings
from hisaab.parsing.email_interpreter import EmailArtifact
from hisaab.parsing.email_spec import CONTRACT_FIELDS
from hisaab.parsing.transforms import collapse_whitespace, strip_html

ANTHROPIC_VERSION: str = "2023-06-01"
TIMEOUT_S: float = 60.0
RETRIES: int = 3
RETRYABLE: frozenset[int] = frozenset({0, 429, 500, 502, 503, 504, 529})  # 0: no response
MAX_BODY_CHARS: int = 20_000
TOOL: str = "record_extraction"

# (method, url, body, headers) -> (status, body); tests pass a fake Claude.
Transport: TypeAlias = Callable[[str, str, bytes | None, dict[str, str]], tuple[int, bytes]]

OUTCOMES: tuple[str, ...] = ("transaction", "non_transactional", "unparseable")
_DATE: re.Pattern[str] = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME: re.Pattern[str] = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
_CURRENCY: re.Pattern[str] = re.compile(r"[A-Z]{3}")
_STRINGS: tuple[str, ...] = ("account_identifier", "counterparty_identifier", "payee_raw", "reference_number")

_NULLABLE_STRING: dict[str, Any] = {"type": ["string", "null"]}
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": list(OUTCOMES)},
        "amount": {"type": ["integer", "null"], "description": "integer paise: Rs. 1,234.50 is 123450"},
        "currency": {"type": ["string", "null"], "description": "ISO 4217 code, e.g. INR"},
        "direction": {"type": ["string", "null"], "enum": ["debit", "credit", None]},
        "account_identifier": {**_NULLABLE_STRING, "description": "the recipient's OWN account or card as printed, e.g. XX1234, or their UPI ID"},
        "counterparty_identifier": {**_NULLABLE_STRING, "description": "the other side's account number or UPI ID, if shown"},
        "payee_raw": {**_NULLABLE_STRING, "description": "merchant or person, exactly as written"},
        "reference_number": {**_NULLABLE_STRING, "description": "UPI reference / transaction id"},
        "date": {**_NULLABLE_STRING, "description": "YYYY-MM-DD"},
        "time": {**_NULLABLE_STRING, "description": "HH:MM, 24-hour, or null"},
        "balance": {"type": ["integer", "null"], "description": "available balance after it, integer paise"},
        "missing_fields": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["outcome", *CONTRACT_FIELDS, "missing_fields"],
}

SYSTEM: str = """You read one email sent to an Indian household and extract at most one money movement.

Call record_extraction exactly once.

outcome:
- "transaction": the email reports money that has ALREADY moved: a debit, a credit, a card purchase, a payment received.
- "non_transactional": no money moved. Marketing, offers, OTPs, "statement is ready", payment reminders, upcoming or scheduled debits, mandate notices, login alerts.
- "unparseable": it reports a money movement, but the amount or the account cannot be read.

For a transaction:
- amount: integer paise (Rs. 1,234.50 -> 123450). Never a decimal.
- direction: "debit" if money left the recipient's account or card, "credit" if it came in.
- account_identifier: the recipient's OWN account or card exactly as the email prints it (e.g. "XX1234", "ending 1234"), or their UPI ID.
- date: the transaction date from the email body as YYYY-MM-DD. If the body gives no date, use the received date given below.
- time: HH:MM 24-hour if the body gives one, else null.
- missing_fields: every contract field you could not find or that was ambiguous. Never guess a value: leave it null and list it.

For non_transactional and unparseable, leave every value you did not read as null."""


class LLMUnavailable(RuntimeError):
    pass


class ContractError(ValueError):
    pass


def default_transport(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, bytes]:
    request: urllib.request.Request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            status: int = response.status
            return status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return 0, b""


def prompt_for(artifact: EmailArtifact) -> str:
    text: str = collapse_whitespace(strip_html(artifact.body))[:MAX_BODY_CHARS]
    received: str = artifact.received_at.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    return f"From: {artifact.sender}\nSubject: {artifact.subject}\nReceived (India time): {received}\n\n{text}"


def extract_email(settings: Settings, artifact: EmailArtifact, *, transport: Transport = default_transport,
                  sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """The §10.4 contract for one email. Raises LLMUnavailable or ContractError."""
    if settings.anthropic_api_key is None:
        raise LLMUnavailable("no LLM configured: set HISAAB_ANTHROPIC_API_KEY in .env")
    body: bytes = json.dumps({
        "model": settings.email_model, "max_tokens": 1024, "system": SYSTEM,
        "tools": [{"name": TOOL, "description": "Record what the email says.", "input_schema": SCHEMA}],
        "tool_choice": {"type": "tool", "name": TOOL},
        "messages": [{"role": "user", "content": prompt_for(artifact)}],
    }).encode()
    headers: dict[str, str] = {"x-api-key": settings.anthropic_api_key.get_secret_value(),
                               "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    url: str = settings.llm_base_url.rstrip("/") + "/v1/messages"
    for attempt in range(RETRIES + 1):
        status, raw = transport("POST", url, body, headers)
        if status not in RETRYABLE:
            break
        if attempt < RETRIES:
            sleep(2 ** attempt)
    else:
        raise LLMUnavailable(f"LLM unavailable: HTTP {status or 'no response'} after {RETRIES} retries")
    if status != 200:
        raise LLMUnavailable(f"LLM refused the request: HTTP {status}")
    reply: dict[str, Any] = json.loads(raw or b"{}")
    for block in reply.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == TOOL:
            return validate_contract(block.get("input") or {})
    raise ContractError(f"the model answered without calling the {TOOL} tool")


def validate_contract(raw: dict[str, Any]) -> dict[str, Any]:
    """The contract with every field typed as PRD §10.4 says, or ContractError naming the field."""
    out: dict[str, Any] = {"outcome": raw.get("outcome")}
    if out["outcome"] not in OUTCOMES:
        raise ContractError(f"outcome: {out['outcome']!r} is not one of {OUTCOMES}")
    for name in ("amount", "balance"):
        value: Any = raw.get(name)
        if value is not None and (type(value) is not int or (name == "amount" and value <= 0)):
            raise ContractError(f"{name}: {value!r} is not {'a positive ' if name == 'amount' else 'an '}integer of paise")
        out[name] = value
    for name, pattern in (("currency", _CURRENCY), ("date", _DATE), ("time", _TIME)):
        text: Any = raw.get(name) or None
        if text is not None and not (isinstance(text, str) and pattern.fullmatch(text)):
            raise ContractError(f"{name}: {text!r} is not in the contract's format")
        out[name] = text
    if out["date"] is not None:
        try:
            date.fromisoformat(out["date"])
        except ValueError:
            raise ContractError(f"date: {out['date']!r} is not a calendar date") from None
    out["direction"] = raw.get("direction") or None
    if out["direction"] not in (None, "debit", "credit"):
        raise ContractError(f"direction: {out['direction']!r} is not debit or credit")
    for name in _STRINGS:
        value = raw.get(name)
        if value is not None and not isinstance(value, str):
            raise ContractError(f"{name}: {value!r} is not text")
        out[name] = (value or "").strip() or None
    missing: Any = raw.get("missing_fields", [])
    if not (isinstance(missing, list) and all(isinstance(m, str) for m in missing)):
        raise ContractError(f"missing_fields: {missing!r} is not a list of field names")
    out["missing_fields"] = missing
    if out["outcome"] == "transaction" and (out["amount"] is None or out["date"] is None):
        raise ContractError("amount and date: a transaction must carry both")
    return out
