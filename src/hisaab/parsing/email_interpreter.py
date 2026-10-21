"""The email interpreter (tech-design §4.3): runs candidate templates over one
email and emits the PRD §10.4 extraction contract, or nothing.

    for each candidate, in the caller's order (priority DESC, promoted_at DESC):
        preprocess the body
        per field: const, else pattern group 1, then the named transform
        a critical field (amount, date, account_identifier) failed?
            -> this template does NOT match; try the next candidate
        otherwise emit the contract; failed required fields go in missing_fields
    no candidate matched -> extraction None: the caller falls through to tier 2

The critical-field gate does two jobs. A stale template cannot consume an email
and emit a broken transaction while a correct template waits behind it; format
changes degrade into the LLM fallback instead. And two templates with the same
sender and subject (the HDFC credit alert and mandate notice share both) are
told apart by body content: the wrong one fails a critical field and declines.

Everything here is pure: no database, no clock. Candidate selection and the
failure counter (§4.3 steps 1 and 9) live in templates.py.
"""

from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.message import EmailMessage, MIMEPart
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

from hisaab.clock import LOCAL_TZ
from hisaab.parsing import patterns
from hisaab.parsing.email_spec import CONTRACT_FIELDS, CRITICAL_FIELDS, EmailSpec, FieldSpec
from hisaab.parsing.transforms import TransformError


# A failure in any of these means the template does not match (§4.3 step 6; confirm: non_transactional specs).
GATE_FIELDS: tuple[str, ...] = (*CRITICAL_FIELDS, "confirm")


@dataclass(frozen=True)
class EmailArtifact:
    sender: str  # bare address, lower-cased
    subject: str
    body: str  # the HTML part if there is one, else plain text
    received_at: datetime  # timezone-aware


def parse_eml(raw: bytes) -> EmailArtifact:
    """An RFC 822 message: an .eml fixture, or Gmail's format=raw after base64url decoding."""
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)
    part: MIMEPart | None = msg.get_body(preferencelist=("html", "plain"))
    return EmailArtifact(
        sender=parseaddr(str(msg["From"] or ""))[1].strip().lower(),
        subject=str(msg["Subject"] or ""),
        body=part.get_content() if part is not None else "",
        received_at=parsedate_to_datetime(str(msg["Date"])),
    )


@dataclass(frozen=True)
class EmailTemplate:
    id: str
    name: str
    spec: EmailSpec


@dataclass(frozen=True)
class FieldFailure:
    field: str
    reason: str  # never contains the captured text: diagnostics outlive the email


@dataclass(frozen=True)
class Attempt:
    template_id: str
    template_name: str
    failures: tuple[FieldFailure, ...]  # required fields only; optional ones just come out null

    @property
    def matched(self) -> bool:
        return not any(f.field in GATE_FIELDS for f in self.failures)


@dataclass(frozen=True)
class Interpretation:
    attempts: tuple[Attempt, ...]  # every template tried, in order: the tier-1 diagnostic (§4.8)
    extraction: dict[str, Any] | None  # the §10.4 contract, or None: no template matched, go to tier 2

    @property
    def template_id(self) -> str | None:
        return self.attempts[-1].template_id if self.extraction is not None else None

    @property
    def needs_review(self) -> bool:
        return bool(self.extraction and self.extraction["missing_fields"])  # §4.3 step 8


def interpret(candidates: list[EmailTemplate], artifact: EmailArtifact) -> Interpretation:
    attempts: list[Attempt] = []
    for template in candidates:
        if template.spec.outcome == "non_transactional":
            failures: list[FieldFailure] = confirm_failures(template.spec, preprocessed(template.spec, artifact))
            attempt: Attempt = Attempt(template.id, template.name, tuple(failures))
            attempts.append(attempt)
            if attempt.matched:  # a notice: the §10.4 contract with nothing extracted
                notice: dict[str, Any] = {"outcome": "non_transactional", **dict.fromkeys(CONTRACT_FIELDS),
                                          "missing_fields": []}
                return Interpretation(tuple(attempts), notice)
            continue
        values, failures = _run(template.spec, artifact)
        attempt = Attempt(template.id, template.name, tuple(failures))
        attempts.append(attempt)
        if attempt.matched:
            missing: list[str] = [f.field for f in failures]  # all non-critical here
            return Interpretation(tuple(attempts), {"outcome": "transaction", **values, "missing_fields": missing})
    return Interpretation(tuple(attempts), None)


def confirm_failures(spec: EmailSpec, text: str) -> list[FieldFailure]:
    """A non_transactional spec's gate: one failure per confirm phrase that is not in the text."""
    failures: list[FieldFailure] = []
    for pattern in spec.confirm:
        if not patterns.matches(pattern, text):
            failures.append(FieldFailure("confirm", f"no match for /{pattern.pattern}/"))
    return failures


@dataclass(frozen=True)
class FieldResult:
    name: str
    value: object  # the contract value, or None
    captured: str | None  # pattern group 1 before the transform (for the authoring harness; never stored)
    reason: str | None  # why it failed; None when extracted or not in this format
    required: bool


def trace(spec: EmailSpec, artifact: EmailArtifact) -> tuple[str, list[FieldResult]]:
    """Run one spec over one email: the preprocessed text, and what happened to each field.
    interpret() is built on this, so the authoring harness shows exactly what production does."""
    text: str = preprocessed(spec, artifact)
    return text, [_field(field, text, artifact) for field in spec.fields]


def preprocessed(spec: EmailSpec, artifact: EmailArtifact) -> str:
    text: str = artifact.body
    for step in spec.preprocess:
        text = step(text)
    return text


def _run(spec: EmailSpec, artifact: EmailArtifact) -> tuple[dict[str, object], list[FieldFailure]]:
    _, results = trace(spec, artifact)
    values: dict[str, object] = {r.name: r.value for r in results}
    failures: list[FieldFailure] = [FieldFailure(r.name, r.reason) for r in results if r.reason and r.required]
    return values, failures


def _field(field: FieldSpec, text: str, artifact: EmailArtifact) -> FieldResult:
    def result(value: object = None, captured: str | None = None, reason: str | None = None) -> FieldResult:
        return FieldResult(field.name, value, captured, reason, field.required)

    if field.const is not None:
        return result(field.const)
    if field.pattern is None:
        if field.name == "date":  # format never states a date: Gmail's received time, in India (ruling 2026-10-10)
            return result(artifact.received_at.astimezone(LOCAL_TZ).date().isoformat())
        return result()
    try:
        m: patterns.Match | None = patterns.search(field.pattern, text)
    except patterns.PatternTimeout as e:
        return result(reason=str(e))
    captured: str | None = m.group(1) if m else None
    if not captured:
        return result(captured=captured, reason="no match")
    try:
        value: object = field.transform(captured) if field.transform else captured
    except TransformError as e:
        return result(captured=captured, reason=str(e))
    if value == "":
        return result(captured=captured, reason="matched only whitespace")
    return result(value, captured)
