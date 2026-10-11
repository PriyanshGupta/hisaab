"""Email template specs (tech-design §4.2) and their save-time validation (§4.5).

A spec is JSON data:

    {"version": 1,
     "match": {"sender": "alerts@bank.in", "subject": "<RE2 pattern>"},  # sender may be a list
     "preprocess": ["strip_html", "collapse_whitespace"],
     "fields": {"amount": {"pattern": "Rs\\.([\\d,.]+) is debited", "transform": "inr_to_paise"},
                "direction": {"const": "debit"},
                "time": {"required": false},
                ...}}

`parse_email_spec` turns it into an EmailSpec ready to run (patterns compiled,
transforms resolved) or raises InvalidSpec listing every problem at once. It is
the single gate: templates.save_email_template calls it before storing, and the
interpreter calls it when loading, so no unvalidated spec ever runs.

`match.sender` is one address or a list (ruling 2026-10-11): banks are moving
alert mail to .bank.in domains with the format unchanged, so one format can
arrive from an old and a new address, and one spec should serve both.

Field rules:
- Every PRD §10.4 contract field is declared, and nothing else. A field the
  format never states is declared {"required": false}, so a forgotten field is
  a save-time error rather than a silent null.
- A field has a `const` (the format implies the value: a debit alert never says
  "debit"), or a `pattern` whose group 1 is the value, optionally with a
  `transform`; or neither, which means "not in this format".
- `required` defaults to true. A required field that fails lands in
  missing_fields and sends the transaction to review (§4.2).
- Critical fields (amount, date, account_identifier) gate the whole template
  (§4.3 step 6), so they must be required patterns. One exception, from the
  2026-10-10 ruling: "date": {"required": false} says the body never states a
  date and the interpreter uses the email's received time in India instead.
- Typed fields must convert to the contract's type: amount and balance through
  inr_to_paise, date through date:<fmt>, time through time:<fmt>.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from hisaab.parsing.patterns import PatternError, compile_pattern
from hisaab.parsing.transforms import UnknownTransform, field_transform, preprocessor

CONTRACT_FIELDS = (
    "amount", "currency", "direction", "account_identifier", "counterparty_identifier",
    "payee_raw", "reference_number", "date", "time", "balance",
)
CRITICAL_FIELDS = ("amount", "date", "account_identifier")
_FIELD_KEYS = {"const", "pattern", "transform", "required"}
# field -> the transform family that yields its contract type
_TYPED = {"amount": "inr_to_paise", "balance": "inr_to_paise", "date": "date:", "time": "time:"}


@dataclass(frozen=True)
class FieldSpec:
    name: str
    const: str | None
    pattern: object | None  # compiled RE2 pattern
    transform: Callable[[str], object] | None
    required: bool


@dataclass(frozen=True)
class EmailSpec:
    senders: tuple[str, ...]  # lower-cased
    subject: object  # compiled RE2 pattern
    preprocess: tuple[Callable[[str], str], ...]
    fields: tuple[FieldSpec, ...]  # in CONTRACT_FIELDS order


class InvalidSpec(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def parse_email_spec(spec: object) -> EmailSpec:
    parsed, problems = check_email_spec(spec)
    if problems:
        raise InvalidSpec(problems)
    return parsed


def check_email_spec(spec: object) -> tuple[EmailSpec, list[str]]:
    """Every problem, plus an EmailSpec of the parts that are valid. Only the authoring
    harness runs a spec that has problems, so a half-written spec still shows results."""
    if not isinstance(spec, dict):
        return EmailSpec((), None, (), ()), ["spec must be a JSON object"]
    problems: list[str] = []

    if spec.get("version") != 1:
        problems.append("version: must be 1")

    match = spec.get("match") if isinstance(spec.get("match"), dict) else {}
    sender = match.get("sender")
    senders = [sender] if isinstance(sender, str) else sender
    if not (isinstance(senders, list) and senders
            and all(isinstance(a, str) and re.fullmatch(r"[^@\s]+@[^@\s]+", a.strip()) for a in senders)):
        problems.append("match.sender: must be an email address or a list of them")
        senders = []
    subject = None
    try:
        subject = compile_pattern(match.get("subject"))
    except PatternError as e:
        problems.append(f"match.subject: {e}")

    steps = []
    preprocess = spec.get("preprocess", [])
    for name in preprocess if isinstance(preprocess, list) else [preprocess]:
        try:
            steps.append(preprocessor(name))
        except (UnknownTransform, TypeError):
            problems.append(f"preprocess: unknown preprocessor '{name}'")

    fields_in = spec.get("fields")
    if not isinstance(fields_in, dict):
        problems.append("fields: must be an object with one entry per contract field")
        fields_in = {}
    for name in fields_in:
        if name not in CONTRACT_FIELDS:
            problems.append(f"{name}: not a contract field (PRD §10.4)")
    fields = []
    for name in CONTRACT_FIELDS:
        if name not in fields_in:
            problems.append(f'{name}: not declared; use {{"required": false}} if the format never states it')
            continue
        field = _parse_field(name, fields_in[name], problems)
        if field:
            fields.append(field)

    parsed = EmailSpec(senders=tuple(a.strip().lower() for a in senders), subject=subject, preprocess=tuple(steps), fields=tuple(fields))
    return parsed, problems


def _parse_field(name: str, raw: object, problems: list[str]) -> FieldSpec | None:
    if not isinstance(raw, dict):
        problems.append(f"{name}: must be an object")
        return None
    before = len(problems)
    for key in raw:
        if key not in _FIELD_KEYS:
            problems.append(f"{name}: unknown key '{key}'")
    const, pattern_text, transform_name = raw.get("const"), raw.get("pattern"), raw.get("transform")
    required = raw.get("required", True)
    if not isinstance(required, bool):
        problems.append(f"{name}: required must be true or false")

    if const is not None and pattern_text is not None:
        problems.append(f"{name}: both const and pattern; a field takes one")
    if transform_name is not None and pattern_text is None:
        problems.append(f"{name}: transform needs a pattern")
    if const is None and pattern_text is None and required is not False:
        problems.append(f'{name}: no pattern or const; mark it {{"required": false}} if the format never states it')

    pattern = transform = None
    if pattern_text is not None:
        try:
            pattern = compile_pattern(pattern_text, groups=1)
        except PatternError as e:
            problems.append(f"{name}: {e}")
    if transform_name is not None:
        try:
            transform = field_transform(transform_name)
        except (UnknownTransform, TypeError, AttributeError):
            problems.append(f"{name}: unknown transform '{transform_name}'")

    if name in CRITICAL_FIELDS:
        date_never_stated = name == "date" and raw == {"required": False}
        if not date_never_stated and (pattern_text is None or required is not True):
            problems.append(f"{name}: critical field, needs a required pattern")
    if name in _TYPED:
        family = _TYPED[name]
        if const is not None:
            problems.append(f"{name}: cannot be a const; it needs a pattern and {family}")
        elif pattern_text is not None and not (isinstance(transform_name, str) and transform_name.startswith(family)):
            problems.append(f"{name}: transform must be {family}{'<format>' if family.endswith(':') else ''}")
    if name == "direction" and const is not None and const not in ("debit", "credit"):
        problems.append("direction: const must be debit or credit")
    if name == "currency" and const is not None and not (isinstance(const, str) and re.fullmatch(r"[A-Z]{3}", const)):
        problems.append("currency: const must be a 3-letter code such as INR")
    if const is not None and not isinstance(const, str):
        problems.append(f"{name}: const must be a string")

    if len(problems) > before:
        return None
    return FieldSpec(name=name, const=const, pattern=pattern, transform=transform, required=required)
