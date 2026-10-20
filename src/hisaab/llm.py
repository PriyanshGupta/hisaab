"""LLM extraction (tech-design §4.1, §4.11): tier-2 emails against the PRD §10.4 contract, and tier-3
statements (an unknown layout) against the same fields per row, inside a statement envelope.

Speaks the Anthropic Messages API at a configurable base URL and model
(HISAAB_LLM_BASE_URL, HISAAB_EMAIL_MODEL, HISAAB_STATEMENT_MODEL), so pointing a tier at a
local server that serves the same API needs no code change. The model must
answer through one forced tool call whose input schema is the contract; the
reply is then validated field by field, and anything off-contract is refused
(ContractError) rather than stored half-right.

LLMUnavailable (no key, network down, overloaded after retries) is not a parse
failure: the Source waits as pending_llm and is retried on the next sync.
"""

import http.client
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
TIMEOUT_S: float = 180.0  # a 100-payee auto-categorize answer with web searches took ~46 s live; never retry a slow answer
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
    return _send(method, url, body, headers, TIMEOUT_S)


def long_transport(timeout: float) -> Transport:
    """default_transport with its own timeout: a statement's answer can run to thousands of tokens."""
    return lambda method, url, body, headers: _send(method, url, body, headers, timeout)


def _send(method: str, url: str, body: bytes | None, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request: urllib.request.Request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status: int = response.status
            return status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        return 0, b""  # incl. a connection dropped mid-answer (IncompleteRead, SSL errors): no response


def prompt_for(artifact: EmailArtifact) -> str:
    text: str = collapse_whitespace(strip_html(artifact.body))[:MAX_BODY_CHARS]
    received: str = artifact.received_at.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    return f"From: {artifact.sender}\nSubject: {artifact.subject}\nReceived (India time): {received}\n\n{text}"


def extract_email(settings: Settings, artifact: EmailArtifact, *, transport: Transport = default_transport,
                  sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """The §10.4 contract for one email. Raises LLMUnavailable or ContractError."""
    return validate_contract(tool_call(settings, SYSTEM, TOOL, "Record what the email says.", SCHEMA,
                                       prompt_for(artifact), transport=transport, sleep=sleep, max_tokens=1024))


WEB_SEARCH: str = "web_search_20250305"  # the basic variant: Haiku has no dynamic-filtering search
PAUSES: int = 3  # a server-side search loop that pauses is resumed at most this many times


def tool_call(settings: Settings, system: str, tool: str, description: str, schema: dict[str, Any], prompt: str, *,
              transport: Transport = default_transport, sleep: Callable[[float], None] = time.sleep,
              max_tokens: int = 1024, search_uses: int = 0, strict: bool = False, model: str | None = None,
              retries: int = RETRIES, forced: bool = True, output_config: dict[str, Any] | None = None
              ) -> dict[str, Any]:
    """One answer from the configured model, through `tool`; its input, unvalidated.

    search_uses > 0 also offers Anthropic's server-side web search, for what the model doesn't recognise. A forced
    tool choice would rule searching out, so the choice is "auto" and the system prompt says to finish with `tool`;
    a search loop that pauses (stop_reason pause_turn) is resumed. If the account can't search (HTTP 400), the
    request is made again without it. Raises LLMUnavailable or ContractError."""
    if settings.anthropic_api_key is None:
        raise LLMUnavailable("no LLM configured: set HISAAB_ANTHROPIC_API_KEY in .env")
    custom: dict[str, Any] = {"name": tool, "description": description, "input_schema": schema,
                              **({"strict": True} if strict else {})}  # strict: the API checks input against schema
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

    def request(searching: bool) -> dict[str, Any]:
        return {"model": model or settings.email_model, "max_tokens": max_tokens, "system": system, "messages": messages,
                "tools": [{"type": WEB_SEARCH, "name": "web_search", "max_uses": search_uses}, custom] if searching
                else [custom],
                "tool_choice": {"type": "auto"} if searching or not forced else {"type": "tool", "name": tool},
                **({"output_config": output_config} if output_config else {})}

    searching: bool = search_uses > 0
    status, reply = _post(settings, request(searching), transport, sleep, retries)
    if status == 400 and searching:  # web search not available to this account: answer from knowledge alone
        searching = False
        status, reply = _post(settings, request(searching), transport, sleep, retries)
    for _ in range(PAUSES):
        if status != 200 or reply.get("stop_reason") != "pause_turn":
            break
        messages.append({"role": "assistant", "content": reply.get("content", [])})  # resume where it paused
        status, reply = _post(settings, request(searching), transport, sleep, retries)
    if status != 200:
        reason: Any = (reply.get("error") or {}).get("message")
        raise LLMUnavailable(f"LLM refused the request: HTTP {status}" + (f": {str(reason)[:300]}" if reason else ""))
    if reply.get("stop_reason") == "max_tokens":  # a cut-off answer is a partial one: never treat it as whole
        raise ContractError(f"the model's {tool} answer was cut off at {max_tokens} tokens")
    for block in reply.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == tool:
            answer: dict[str, Any] = block.get("input") or {}
            return answer
    raise ContractError(f"the model answered without calling the {tool} tool")


def _post(settings: Settings, body: dict[str, Any], transport: Transport, sleep: Callable[[float], None],
          retries: int = RETRIES) -> tuple[int, dict[str, Any]]:
    """POST /v1/messages with retries on overload and network errors; (status, parsed body)."""
    assert settings.anthropic_api_key is not None
    headers: dict[str, str] = {"x-api-key": settings.anthropic_api_key.get_secret_value(),
                               "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    url: str = settings.llm_base_url.rstrip("/") + "/v1/messages"
    data: bytes = json.dumps(body).encode()
    for attempt in range(retries + 1):
        status, raw = transport("POST", url, data, headers)
        if status not in RETRYABLE:
            break
        if attempt < retries:
            sleep(2 ** attempt)
    else:
        raise LLMUnavailable(f"LLM unavailable: HTTP {status or 'no response'} after {retries} retries")
    if status != 200:  # keep the API's own reason (it describes the request, never the email): {"error": {...}}
        try:
            error: Any = json.loads(raw or b"{}").get("error")
        except (json.JSONDecodeError, AttributeError):
            error = None
        return status, {"error": error} if isinstance(error, dict) else {}
    try:
        parsed: dict[str, Any] = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise ContractError("the model's reply wasn't JSON") from None
    return status, parsed


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


# --- tier 3: statements ---------------------------------------------------------------------------------

STATEMENT_TOOL: str = "record_statement"
STATEMENT_TIMEOUT_S: float = 600.0  # a long statement is thousands of output tokens
STATEMENT_MAX_TOKENS: int = 32_000
STATEMENT_RETRIES: int = 0  # a read can take ten minutes: a retry would double the wait; the person re-uploads
STATEMENT_EFFORT: str = "medium"  # thinking counts against max_tokens; transcribing rows needs little of it
MAX_STATEMENT_CHARS: int = 400_000
_LAST4: re.Pattern[str] = re.compile(r"\d{4}")

_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "page": {"type": "integer", "description": "the page number the row is printed on"},
        "date": {"type": "string", "description": "YYYY-MM-DD"},
        "time": {**_NULLABLE_STRING, "description": "HH:MM, 24-hour, or null"},
        "narration": {"type": "string", "description": "the description exactly as printed"},
        "reference": {**_NULLABLE_STRING, "description": "the reference / cheque number column, as printed"},
        "direction": {"type": "string", "enum": ["debit", "credit"]},
        "amount": {"type": "integer", "description": "integer paise, exactly as printed: 1,234.50 is 123450"},
        "balance": {"type": ["integer", "null"], "description": "the running balance printed on the row, paise"},
        "bill_payment": {"type": "boolean", "description": "card statements only: a payment of this card's bill"},
    },
    "required": ["page", "date", "time", "narration", "reference", "direction", "amount", "balance", "bill_payment"],
    "additionalProperties": False,
}
STATEMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["statement", "not_a_statement"]},
        "account_kind": {**_NULLABLE_STRING, "description": "bank or credit_card"},
        "account_last4": {**_NULLABLE_STRING, "description": "last 4 digits of the statement's own account or card"},
        "institution": {**_NULLABLE_STRING, "description": "the bank or card issuer"},
        "opening": {"type": ["integer", "null"], "description": "opening balance as printed, paise (card: owed)"},
        "closing": {"type": ["integer", "null"], "description": "closing balance as printed, paise (card: total due)"},
        "rows": {"type": "array", "items": _ROW_SCHEMA},
        "unreadable": {"type": "array", "items": {"type": "object", "properties": {
            "page": {"type": "integer"}, "text": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["page", "text", "reason"], "additionalProperties": False}},
    },
    "required": ["outcome", "account_kind", "account_last4", "institution", "opening", "closing", "rows", "unreadable"],
    "additionalProperties": False,
}

STATEMENT_SYSTEM: str = """You read one Indian bank or credit card statement, given as the text of each page with its columns kept in place, and record every transaction row.

Call record_statement exactly once.

- outcome: "statement" for a bank account or credit card statement; "not_a_statement" for anything else (then rows is empty).
- account_kind: "bank" or "credit_card". account_last4: the last 4 digits of the statement's own account or card number.
- opening and closing: the balances the statement itself prints (a card's previous dues and total amount due, as positive amounts owed). Integer paise. null if not printed. Never compute them.
- rows: every transaction row of the statement, in order, across all pages. Summary lines, totals, reward points, offers and page headers are not rows.
  - amount: integer paise, exactly as printed on that row (1,234.50 -> 123450). Never a sum, never a correction.
  - direction: from the column the amount sits in, or its Cr/Dr (C/D) marker, or its sign. On a card statement, purchases are debits; payments, refunds and cashback are credits.
  - date: YYYY-MM-DD; the transaction date when the statement gives two dates.
  - narration: the description exactly as printed, continuation lines joined.
  - bill_payment: true only for a credit on a credit card statement that is a payment of that card's bill; false for refunds, cashback and everything on a bank statement.
  - page: the page the row is printed on.
- unreadable: any line in a transaction table you cannot read with certainty (which column, which amount, which date). Put it here with the reason instead of guessing."""


def extract_statement(settings: Settings, pages: list[str], *, transport: Transport | None = None,
                      sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Tier 3: a statement in an unknown layout, page texts in, the validated envelope out. Not cached (ruling
    2026-10-19: every upload is a deliberate re-parse, amendment 7). Raises LLMUnavailable or ContractError."""
    prompt: str = "\n\n".join(f"=== Page {i} ===\n{text}" for i, text in enumerate(pages, start=1))
    if len(prompt) > MAX_STATEMENT_CHARS:
        raise ContractError("statement: too long to send to the model")
    def ask() -> dict[str, Any]:
        # tool_choice "auto": the 5.5 models refuse forced tool use; strict keeps the arguments schema-valid
        return tool_call(settings, STATEMENT_SYSTEM, STATEMENT_TOOL, "Record the statement's account, balances and "
                         "every row.", STATEMENT_SCHEMA, prompt, transport=transport or long_transport(STATEMENT_TIMEOUT_S),
                         sleep=sleep, max_tokens=STATEMENT_MAX_TOKENS, model=settings.statement_model,
                         retries=STATEMENT_RETRIES, forced=False, strict=True,
                         output_config={"effort": STATEMENT_EFFORT})
    try:
        raw: dict[str, Any] = ask()
    except ContractError as e:
        if "without calling" not in str(e):
            raise
        raw = ask()  # "auto" doesn't guarantee the call: ask once more, then refuse
    return validate_statement(raw, page_count=len(pages))


def _row_problem(row: Any, page_count: int) -> str | None:
    """Why one row breaks the contract, or None."""
    if not isinstance(row, dict):
        return "the row is not an object"
    page: Any = row.get("page")
    if type(page) is not int or not 1 <= page <= page_count:
        return f"page {page!r} is not a page of this statement"
    when: Any = row.get("date")
    if not (isinstance(when, str) and _DATE.fullmatch(when)):
        return f"date {when!r} is not YYYY-MM-DD"
    try:
        date.fromisoformat(when)
    except ValueError:
        return f"date {when!r} is not a calendar date"
    clock: Any = row.get("time")
    if clock is not None and not (isinstance(clock, str) and _TIME.fullmatch(clock)):
        return f"time {clock!r} is not HH:MM"
    amount: Any = row.get("amount")
    if type(amount) is not int or amount <= 0:
        return f"amount {amount!r} is not a positive integer of paise"
    balance: Any = row.get("balance")
    if balance is not None and type(balance) is not int:
        return f"balance {balance!r} is not an integer of paise"
    if row.get("direction") not in ("debit", "credit"):
        return f"direction {row.get('direction')!r} is not debit or credit"
    if not isinstance(row.get("narration"), str):
        return "narration is not text"
    reference: Any = row.get("reference")
    if reference is not None and not isinstance(reference, str):
        return "reference is not text"
    return None


def validate_statement(raw: dict[str, Any], page_count: int) -> dict[str, Any]:
    """The envelope typed, or ContractError naming the field; a malformed ROW is moved to unreadable with its
    reason, so one bad row costs that row, never the statement (it shows in the preview's third group)."""
    outcome: Any = raw.get("outcome")
    if outcome not in ("statement", "not_a_statement"):
        raise ContractError(f"outcome: {outcome!r} is not statement or not_a_statement")
    kind: Any = raw.get("account_kind")
    if kind not in (None, "bank", "credit_card"):
        raise ContractError(f"account_kind: {kind!r} is not bank or credit_card")
    last4: Any = raw.get("account_last4")
    if last4 is not None and not (isinstance(last4, str) and _LAST4.fullmatch(last4)):
        raise ContractError(f"account_last4: {last4!r} is not 4 digits")
    institution: Any = raw.get("institution")
    if institution is not None and not isinstance(institution, str):
        raise ContractError("institution: not text")
    for name in ("opening", "closing"):
        if raw.get(name) is not None and type(raw.get(name)) is not int:
            raise ContractError(f"{name}: {raw.get(name)!r} is not an integer of paise")
    rows: Any = raw.get("rows")
    lines: Any = raw.get("unreadable", [])
    if not isinstance(rows, list):
        raise ContractError("rows: not a list")
    if not (isinstance(lines, list) and all(isinstance(x, dict) for x in lines)):
        raise ContractError("unreadable: not a list of lines")
    if outcome == "not_a_statement" and rows:
        raise ContractError("rows: a file that isn't a statement has no rows")
    good: list[dict[str, Any]] = []
    unreadable: list[dict[str, Any]] = [
        {"page": x.get("page") if type(x.get("page")) is int else 0, "text": str(x.get("text", "")),
         "reason": str(x.get("reason", ""))} for x in lines]
    for row in rows:
        problem: str | None = _row_problem(row, page_count)
        if problem is None:
            good.append({"page": row["page"], "date": row["date"], "time": row.get("time"),
                         "narration": row["narration"].strip(), "reference": (row.get("reference") or "").strip() or None,
                         "direction": row["direction"], "amount": row["amount"], "balance": row.get("balance"),
                         "bill_payment": row.get("bill_payment") is True})
        else:
            page: Any = row.get("page") if isinstance(row, dict) else None
            text: str = " ".join(str(row.get(k, "")) for k in ("date", "narration", "amount")) if isinstance(row, dict) \
                else str(row)
            unreadable.append({"page": page if type(page) is int else 0, "text": text, "reason": problem})
    return {"outcome": outcome, "account_kind": kind, "account_last4": last4, "institution": institution,
            "opening": raw.get("opening"), "closing": raw.get("closing"), "rows": good, "unreadable": unreadable}


# --- template proposals (tech-design §4.10 step 2) ------------------------------------------------------------

PROPOSAL_TOOL: str = "propose_template"
PROPOSAL_MAX_TOKENS: int = 8_000
_PROPOSAL_FIELD: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["pattern", "const", "absent"]},
        # plain strings, empty when unused: strict tools allow at most 16 nullable (union-typed) parameters
        "pattern": {"type": "string", "description": "RE2; capture group 1 is the value. Empty unless kind is pattern"},
        "transform": {"type": "string", "description": "a transform name from the list, or empty"},
        "value": {"type": "string", "description": "the constant. Empty unless kind is const"},
        "required": {"type": "boolean"},
    },
    "required": ["kind", "pattern", "transform", "value", "required"],
    "additionalProperties": False,
}
PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "snake_case, bank_product_kind, e.g. lantern_card_purchase"},
        "subject_pattern": {"type": "string", "description": "RE2 matched against the subject"},
        "fields": {"type": "object", "properties": {name: _PROPOSAL_FIELD for name in CONTRACT_FIELDS},
                   "required": list(CONTRACT_FIELDS), "additionalProperties": False},
    },
    "required": ["name", "subject_pattern", "fields"],
    "additionalProperties": False,
}
PROPOSAL_SYSTEM: str = """You write a parsing template for one bank alert email format, from three examples of it.

Each example gives the email's sender, subject and received time, the text your patterns will run against
(HTML already stripped, whitespace collapsed), and the reading another model made of it. The template is
run on all three and must reproduce that reading EXACTLY, field by field, character for character. Do not
correct the reading: copy its conventions (for example, if account_identifier is "XX1234", capture "XX1234";
if it is "1234", capture only the digits).

Call propose_template once.

- subject_pattern: an RE2 pattern anchored on the subject's fixed words (start with ^), never on amounts,
  dates or names.
- For each field choose a kind:
  - "pattern": an RE2 pattern whose capture group 1 is the value, anchored on fixed words around it. RE2 has no
    lookarounds and no backreferences. Keep patterns short (under 200 characters).
  - "const": the format implies the value without stating it, e.g. direction "debit" for a debit alert, currency
    "INR". Put it in value.
  Leave pattern, transform and value as empty strings where they don't apply.
  - "absent": the format never states it (required false). For date, "absent" means "use the email's
    received date", which is right only when the body states no date and the reading's date is the received
    date in India.
- transform (or an empty string for none), from this list only:
  - inr_to_paise: "1,250.00" -> 125000. REQUIRED for amount and balance.
  - date:<strptime format>, e.g. date:%d-%m-%y or date:%d %b %Y. REQUIRED for a date pattern; the result is YYYY-MM-DD.
  - time:<strptime format>, e.g. time:%H:%M:%S. REQUIRED for a time pattern; the result is HH:MM:SS, compared
    with the reading to the minute. Capture the time exactly as printed, seconds included if shown.
  - digits_only, normalize_vpa, upper, trim.
- amount, date and account_identifier must be required patterns (date may instead be "absent").
- required: true when every email of this format states the field; false when it is sometimes missing.
- A field whose reading is null in all three examples is "absent" with required false."""


def propose_template(settings: Settings, examples: list[tuple[EmailArtifact, dict[str, Any]]], *,
                     transport: Transport = default_transport,
                     sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Sonnet's form for a template over three examples (artifact, cached reading). The answer is checked only
    for shape here; promotion.propose builds the spec and replay validates it. Raises LLMUnavailable or
    ContractError."""
    prompt: str = "\n\n".join(
        f"=== Email {n} ===\n{prompt_for(artifact)}\n\n--- The reading to reproduce ---\n"
        f"{json.dumps({k: reading.get(k) for k in ('outcome', *CONTRACT_FIELDS)}, ensure_ascii=False)}"
        for n, (artifact, reading) in enumerate(examples, start=1))

    def ask() -> dict[str, Any]:
        # tool_choice "auto": the 5.5 models refuse forced tool use; strict keeps the arguments schema-valid
        return tool_call(settings, PROPOSAL_SYSTEM, PROPOSAL_TOOL, "Record the template.", PROPOSAL_SCHEMA, prompt,
                         transport=transport, sleep=sleep, max_tokens=PROPOSAL_MAX_TOKENS,
                         model=settings.template_model, forced=False, strict=True)
    try:
        answer: dict[str, Any] = ask()
    except ContractError as e:
        if "without calling" not in str(e):
            raise
        answer = ask()  # "auto" doesn't guarantee the call: ask once more, then refuse
    fields: Any = answer.get("fields")
    if not isinstance(answer.get("subject_pattern"), str) or not isinstance(fields, dict) \
            or set(fields) != set(CONTRACT_FIELDS) or not all(isinstance(v, dict) for v in fields.values()):
        raise ContractError("propose_template: the answer is missing its subject pattern or a field")
    return answer
