"""The spec authoring harness (build-plan slice 9): `python -m hisaab try-template`.

    python -m hisaab try-template SPEC.json FIXTURE.eml [FIXTURE.eml ...] [--text]

Runs one spec over each fixture and prints, per field, what the pattern
captured and what the transform made of it, or why it failed. Then the
verdict the interpreter would reach. The loop is: --text to see the text the
patterns run against, edit the spec, run again.

Nothing here matches anything itself. Per-field detail comes from
email_interpreter.trace and the verdict from interpret, the same functions
production runs, so what this prints is what production does.

    python -m hisaab template-skeleton FIXTURE.eml > SPEC.json

writes a starting spec from one email: its sender, its exact subject, the
usual preprocessing, and every contract field declared. The fields left to
write show up in try-template as "pattern is empty".

Exit status (try-template): 0 when every fixture matched with nothing missing, 1 when any did
not, 2 when the input can't be used (unreadable spec or fixture). A spec with
"artifact_type": "statement" runs in statement mode (statement_harness.py).
"""

import json
import re
import textwrap
from pathlib import Path
from typing import Any, Protocol, TextIO

from hisaab.parsing import patterns
from hisaab.parsing.email_interpreter import (
    GATE_FIELDS,
    EmailArtifact,
    EmailTemplate,
    FieldResult,
    Interpretation,
    confirm_failures,
    interpret,
    parse_eml,
    trace,
)
from hisaab.parsing.email_spec import CONTRACT_FIELDS, CRITICAL_FIELDS, EmailSpec, check_email_spec


class Say(Protocol):
    """Prints one line of the report (no argument: a blank line)."""

    def __call__(self, line: str = "") -> None: ...


def try_template(spec_path: str, fixture_paths: list[str], show_text: bool, out: TextIO,
                 password_env: str | None = None) -> int:
    def say(line: str = "") -> None:
        print(line, file=out)

    try:
        raw: object = json.loads(Path(spec_path).read_text())
    except OSError as e:
        say(f"Can't read {spec_path}: {e.strerror}")
        return 2
    except json.JSONDecodeError as e:
        say(f"{spec_path} is not valid JSON: {e.msg} at line {e.lineno}, column {e.colno}")
        return 2
    if isinstance(raw, dict) and raw.get("artifact_type") == "statement":
        from hisaab.parsing.statement_harness import try_statement

        return try_statement(raw, fixture_paths, show_text, password_env, say)

    spec, problems = check_email_spec(raw)
    if problems:
        say("Spec problems; this template cannot be saved until they are fixed:")
        for p in problems:
            say(f"  - {p}")
        say("Fields with problems are not run below.")

    worst: int = 0
    for path in fixture_paths:
        try:
            artifact: EmailArtifact = parse_eml(Path(path).read_bytes())
        except OSError as e:
            say(f"\nCan't read {path}: {e.strerror}")
            return 2
        except (TypeError, ValueError) as e:  # no Date header, unparseable message
            say(f"\nCan't read {path} as an email: {e}")
            return 2
        say(f"\n=== {path}")
        ok: bool = _report(spec, problems, artifact, show_text, say)
        worst = max(worst, 0 if ok else 1)
    return worst


def _report(spec: EmailSpec, problems: list[str], artifact: EmailArtifact, show_text: bool, say: Say) -> bool:
    sender_ok: bool = artifact.sender in spec.senders
    say(f"  sender   {artifact.sender}  {'ok' if sender_ok else f'not in spec {list(spec.senders)}'}")
    subject_ok: bool = spec.subject is not None and patterns.matches(spec.subject, artifact.subject)
    say(f"  subject  {artifact.subject!r}  {'ok' if subject_ok else 'does not match the spec subject pattern'}")
    tried: bool = sender_ok and subject_ok
    if not tried:
        say("  -> production would never try this template on this email")

    text, results = trace(spec, artifact)
    if show_text:
        say("\n  text the patterns run against:")
        for line in textwrap.wrap(text, 100):
            say(f"    {line}")
    say()
    if spec.outcome == "non_transactional":
        missing: set[str] = {f.reason for f in confirm_failures(spec, text)}
        for pattern in spec.confirm:
            ok: bool = f"no match for /{pattern.pattern}/" not in missing
            say(f"  confirm  {'ok' if ok else 'x '} /{pattern.pattern}/")
    else:
        by_name: dict[str, FieldResult] = {r.name: r for r in results}
        for name in CONTRACT_FIELDS:
            say(f"  {name:24} {_describe(name, by_name.get(name), problems)}")
    say()

    if problems:
        say("  NOT MATCHED: the spec has problems (above), so it cannot run in production.")
        return False
    result: Interpretation = interpret([EmailTemplate("harness", "harness", spec)], artifact)
    if not tried:
        say("  NOT TRIED: the sender or subject does not match, so production never runs this template on this "
            "email, whatever the fields above show.")
        return False
    if result.extraction is None:
        failed: list[str] = sorted({f.field for f in result.attempts[0].failures if f.field in GATE_FIELDS})
        say(f"  NOT MATCHED: {', '.join(failed)} failed. Production would try the next "
            "template, then fall through to tier 2.")
        return False
    if result.extraction["outcome"] == "non_transactional":
        say("  MATCHED: non-transactional (a notice; no money moved, nothing recorded).")
        return True
    if result.needs_review:
        say(f"  MATCHED, needs review: missing_fields = {result.extraction['missing_fields']}")
        return False
    say("  MATCHED: every field extracted.")
    return True


def skeleton(fixture_path: str) -> dict[str, Any]:
    artifact: EmailArtifact = parse_eml(Path(fixture_path).read_bytes())
    empty: str = ""  # try-template lists these as "pattern is empty": the to-do list
    return {
        "version": 1,
        "match": {"sender": artifact.sender, "subject": f"^{re.escape(artifact.subject).replace('\\ ', ' ')}$"},
        "preprocess": ["strip_html", "collapse_whitespace"],
        "fields": {
            "amount": {"pattern": empty, "transform": "inr_to_paise"},
            "currency": {"const": "INR"},
            "direction": {"const": "debit"},
            "account_identifier": {"pattern": empty, "transform": "digits_only"},
            "counterparty_identifier": {"required": False},
            "payee_raw": {"pattern": empty, "transform": "trim"},
            "reference_number": {"required": False},
            "date": {"pattern": empty, "transform": "date:%d-%m-%y"},
            "time": {"required": False},
            "balance": {"required": False},
        },
    }


def _describe(name: str, r: FieldResult | None, problems: list[str]) -> str:
    if r is None:
        mine: list[str] = [p.split(": ", 1)[1] for p in problems if p.startswith(f"{name}: ")]
        return f"?  not run: {'; '.join(mine) or 'spec problem'}"
    if r.reason:
        label: str = "critical" if name in CRITICAL_FIELDS else ("required" if r.required else "optional, so null")
        captured: str = f'"{r.captured}": ' if r.captured else ""
        return f"x  {label}: {captured}{r.reason}"
    if r.captured is not None:
        return f'ok "{r.captured}" -> {r.value!r}'
    if r.value is not None:
        return f"ok const -> {r.value!r}" if name != "date" else f"ok received time (no date in body) -> {r.value!r}"
    return "-  not in this format"
