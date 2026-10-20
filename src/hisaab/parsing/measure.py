"""The fixture measurement behind PRD §9 (build-plan slice 11).

    python -m hisaab measure-fixtures [--fixtures DIR]

PRD §9: "95%+ of transactions from a known format parse with correct amount,
date, and account", measured against the fixture corpus, run by the developer
before each release. This runs every fixture that has a hand-checked answer
beside it (<name>.expected.json; fixtures are local-only) through the
production path: the seed templates, candidate selection by sender and
subject, the interpreters. Then it compares.

- Email: one transaction. Correct when amount, date and account_identifier all
  equal the answer. Other fields that differ are listed but do not fail it
  (they are outside the criterion). An email no template matched is wrong: in
  production it falls through to tier 2, the LLM.
- Notice (expected outcome non_transactional): no transaction; it passes when
  it is classified non-transactional.
- Statement: one transaction per expected row, compared in order on date,
  direction and amount, plus the account the statement binds to. A statement
  that cannot bind its account (the preview would ask) gets every row's
  account wrong: strict, because "correct account" is what the PRD counts.

Templates approved in the promotion loop (slice 22) are measured too: approval exports their three
examples with the spec beside them (promoted/<name>.template.json), loaded with the seeds.

The rate is correct transactions over all transactions. Hold-outs (examples
kept out of spec authoring) are also reported on their own.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hisaab.parsing.email_interpreter import EmailArtifact, EmailTemplate, Interpretation, interpret, parse_eml
from hisaab.parsing.email_spec import parse_email_spec, same_value
from hisaab.parsing.pdf import Page, PdfError, read_pdf
from hisaab.parsing.statement_interpreter import (
    StatementResult,
    StatementTemplate,
    TemplateMismatch,
    interpret_statement,
    pick_statement_template,
)
from hisaab.parsing.statement_spec import parse_statement_spec
from hisaab.parsing.templates import select_email_candidates

CRITERION: tuple[str, ...] = ("amount", "date", "account_identifier")
_EXAMPLE_SUFFIX: re.Pattern[str] = re.compile(r"_\d+$")


@dataclass(frozen=True)
class Templates:
    email: list[EmailTemplate]
    statement: list[StatementTemplate]


@dataclass(frozen=True)
class FixtureResult:
    name: str  # fixture file stem, e.g. hdfc_cc_purchase_02
    format: str  # e.g. hdfc_cc_purchase
    kind: str  # email | notice | statement
    held_out: bool
    template: str | None  # the template that parsed it, None if none did
    transactions: int
    correct: int
    detail: str

    @property
    def ok(self) -> bool:
        if self.kind == "notice":
            return self.template is not None and not self.detail
        return self.correct == self.transactions


@dataclass(frozen=True)
class Summary:
    transactions: int
    correct: int
    held_out_transactions: int
    held_out_correct: int

    @property
    def rate(self) -> float:
        return self.correct / self.transactions if self.transactions else 0.0


def load_seed_templates(seed_dir: Path) -> Templates:
    """Every seed spec, named after its file, in name order (seeds all have priority 0)."""
    email: list[EmailTemplate] = []
    statement: list[StatementTemplate] = []
    for path in sorted(seed_dir.glob("*.json")):
        spec: dict[str, Any] = json.loads(path.read_text())
        if spec.get("artifact_type") == "statement":
            statement.append(StatementTemplate(path.stem, path.stem, parse_statement_spec(spec)))
        else:
            email.append(EmailTemplate(path.stem, path.stem, parse_email_spec(spec)))
    return Templates(email, statement)


def load_templates(seed_dir: Path, fixtures_dir: Path) -> Templates:
    """The seeds, plus the promoted templates exported beside their fixtures."""
    seeds: Templates = load_seed_templates(seed_dir)
    return Templates(seeds.email + load_promoted_templates(fixtures_dir), seeds.statement)


def load_promoted_templates(fixtures_dir: Path) -> list[EmailTemplate]:
    """Templates approved in the promotion loop live in the database, not the seeds; approval writes each one's
    spec beside its exported fixtures (<name>.template.json), so those fixtures are measured with it."""
    return [EmailTemplate(path.name.removesuffix(".template.json"), path.name.removesuffix(".template.json"),
                          parse_email_spec(json.loads(path.read_text())))
            for path in sorted(fixtures_dir.rglob("*.template.json"))]


def measure(fixtures_dir: Path, templates: Templates, held_out: frozenset[str]) -> list[FixtureResult]:
    results: list[FixtureResult] = []
    for path in sorted(fixtures_dir.rglob("*.eml")):
        expected_path: Path = path.with_suffix(".expected.json")
        if expected_path.is_file():
            results.append(_email(path, json.loads(expected_path.read_text()), templates, held_out))
    for path in sorted(fixtures_dir.rglob("*.pdf")):
        expected_path = path.with_suffix(".expected.json")
        if expected_path.is_file():
            results.append(_statement(path, json.loads(expected_path.read_text()), templates, held_out))
    return results


def summarize(results: list[FixtureResult]) -> Summary:
    held: list[FixtureResult] = [r for r in results if r.held_out]
    return Summary(sum(r.transactions for r in results), sum(r.correct for r in results),
                   sum(r.transactions for r in held), sum(r.correct for r in held))


def _format(path: Path) -> str:
    return _EXAMPLE_SUFFIX.sub("", path.stem)


def _email(path: Path, expected: dict[str, Any], templates: Templates, held_out: frozenset[str]) -> FixtureResult:
    artifact: EmailArtifact = parse_eml(path.read_bytes())
    result: Interpretation = interpret(select_email_candidates(templates.email, artifact.sender, artifact.subject),
                                       artifact)
    template: str | None = None
    if result.template_id is not None:
        template = next(t.name for t in templates.email if t.id == result.template_id)
    notice: bool = expected.get("outcome") == "non_transactional"
    name: str = path.stem
    base: dict[str, Any] = {"name": name, "format": _format(path), "held_out": name in held_out, "template": template}
    if result.extraction is None:
        tried: str = ", ".join(a.template_name for a in result.attempts) or "no candidate template"
        return FixtureResult(**base, kind="notice" if notice else "email", transactions=0 if notice else 1, correct=0,
                             detail=f"no template matched (tried: {tried}); production falls through to tier 2")
    if notice:
        detail: str = "" if result.extraction["outcome"] == "non_transactional" else "parsed as a transaction"
        return FixtureResult(**base, kind="notice", transactions=0, correct=0, detail=detail)
    wrong: list[str] = [k for k in expected
                        if k != "missing_fields" and not same_value(k, expected[k], result.extraction.get(k))]
    critical: list[str] = [k for k in wrong if k in CRITERION]
    detail = "; ".join(f"{k}: got {result.extraction.get(k)!r}, expected {expected[k]!r}" for k in wrong)
    return FixtureResult(**base, kind="email", transactions=1, correct=0 if critical else 1, detail=detail)


def _statement(path: Path, expected: dict[str, Any], templates: Templates, held_out: frozenset[str]) -> FixtureResult:
    rows_expected: list[list[Any]] = expected["rows"]
    name: str = path.stem
    base: dict[str, Any] = {"name": name, "format": _format(path), "kind": "statement", "held_out": name in held_out,
                            "transactions": len(rows_expected)}
    try:
        pages: list[Page] = read_pdf(path.read_bytes(), os.environ.get("HISAAB_TEST_PDF_PASSWORD"))
    except PdfError as e:
        return FixtureResult(**base, template=None, correct=0, detail=str(e))
    chosen: StatementTemplate | None = pick_statement_template(templates.statement, pages)
    if chosen is None:
        return FixtureResult(**base, template=None, correct=0, detail="no template detected; production goes to tier 3")
    try:
        result: StatementResult = interpret_statement(chosen.spec, pages)
    except TemplateMismatch as e:
        return FixtureResult(**base, template=chosen.name, correct=0, detail=f"does not fit: {e}")
    account_ok: bool = list(result.account) == expected["account"] if result.account else expected["account"] is None
    got: list[list[Any]] = [[r.date, r.direction, r.amount] for r in result.rows]
    row_ok: list[bool] = [i < len(got) and got[i] == want for i, want in enumerate(rows_expected)]
    correct: int = sum(row_ok) if account_ok else 0
    notes: list[str] = []
    if not account_ok:
        notes.append(f"account: got {list(result.account) if result.account else None}, "
                     f"expected {expected['account']}")
    if len(got) != len(rows_expected):
        notes.append(f"{len(got)} rows parsed, {len(rows_expected)} expected")
    bad: list[int] = [i + 1 for i, ok in enumerate(row_ok) if not ok]
    if bad:
        notes.append(f"rows wrong: {bad[:10]}")
    notes.append(f"validation {result.validation['outcome']}")
    return FixtureResult(**base, template=chosen.name, correct=correct, detail="; ".join(notes))
