"""compare-tiers: one statement read by its template (tier 1) and by the model (tier 3), side by side.

The live check that tier 3 reads statements correctly (ruling 2026-10-19). Rows are compared on what the
money check depends on, (date, direction, amount), as multisets so two equal rows count twice; narrations
are only counted when they differ (the model may join continuation lines differently). Exit 0 when both
tiers have the same rows and the same check outcome, 1 when they differ, 2 when there is nothing to compare.

Runs on the owner's own downloaded statements only: fixtures are unredacted and never go to an LLM
(CLAUDE.md), so a path under fixtures/ is refused before the file is opened. Nothing is written.
"""

import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO, TypeAlias

from hisaab.config import PROJECT_ROOT, Settings
from hisaab.db import Database
from hisaab.llm import ContractError, LLMUnavailable, Transport
from hisaab.parsing.pdf import Page, PdfError, read_pdf
from hisaab.parsing.statement_harness import rupees
from hisaab.parsing.statement_interpreter import (
    StatementResult,
    StatementRow,
    StatementTemplate,
    TemplateMismatch,
    interpret_statement,
    pick_statement_template,
)
from hisaab.parsing.templates import statement_candidates
from hisaab.statements.tier3 import ModelRead, NotAStatement, read_with_model

Key: TypeAlias = tuple[str, str, int]  # (date, direction, amount)


def _key(row: StatementRow) -> Key:
    return (row.date, row.direction, row.amount)


def _summary(name: str, result: StatementResult) -> str:
    v: dict[str, Any] = result.validation
    gap: str = f" (gap {rupees(abs(v['gap_paise']))})" if v["gap_paise"] is not None else ""
    count: str = f"{len(result.rows)} row{'' if len(result.rows) == 1 else 's'}"
    return f"{name}: {count}, check {v['outcome']}{gap}, {len(result.unparseable)} couldn't be read"


def compare_tiers(db: Database, settings: Settings, pdf: Path, password: str | None, out: TextIO, *,
                  transport: Transport | None = None, sleep: Callable[[float], None] = time.sleep,
                  fixtures_dir: Path = PROJECT_ROOT / "fixtures") -> int:
    def say(line: str) -> None:
        print(line, file=out)

    if pdf.resolve().is_relative_to(fixtures_dir.resolve()):
        say("Fixtures are unredacted and never go to an LLM (CLAUDE.md). Use a statement you downloaded.")
        return 2
    try:
        pages: list[Page] = read_pdf(pdf.read_bytes(), password)
    except PdfError as e:
        say(str(e))
        return 2
    with db.read_session() as s:
        template: StatementTemplate | None = pick_statement_template(statement_candidates(s), pages)
    if template is None:
        say("No known layout reads this statement, so there's nothing to compare Claude's reading against.")
        return 2
    try:
        tier1: StatementResult = interpret_statement(template.spec, pages, template_id=template.id,
                                                     template_name=template.name)
    except TemplateMismatch as e:
        say(f"The “{template.name}” layout didn't fit ({e}), so there's nothing to compare against.")
        return 2
    started: float = time.monotonic()
    try:
        tier3: ModelRead = read_with_model(settings, pages, transport=transport, sleep=sleep)
    except (LLMUnavailable, ContractError, NotAStatement) as e:
        say(f"Claude couldn't read it: {e}")
        return 1
    seconds: float = time.monotonic() - started
    say(_summary(f"Template “{template.name}”", tier1))
    say(_summary("Claude", tier3.result))
    ones: Counter[Key] = Counter(_key(r) for r in tier1.rows)
    threes: Counter[Key] = Counter(_key(r) for r in tier3.result.rows)
    for label, missing in (("Only the template has", ones - threes), ("Only Claude has", threes - ones)):
        for (day, direction, amount), n in sorted(missing.items()):
            say(f"{label}: {day} {direction} {rupees(amount)}" + (f" (×{n})" if n > 1 else ""))
    same: bool = ones == threes
    if same:
        say(f"Same {len(tier1.rows)} rows in both.")
    narrations: dict[Key, list[str]] = {}
    for r in tier1.rows:
        narrations.setdefault(_key(r), []).append(" ".join(r.narration.split()))
    differ: int = sum(1 for r in tier3.result.rows
                      if _key(r) in narrations and " ".join(r.narration.split()) not in narrations[_key(r)])
    if differ:
        say("1 row's narration differs." if differ == 1 else f"{differ} rows' narrations differ.")
    say(f"Claude took {seconds:.0f} s ({settings.statement_model}).")
    return 0 if same and tier1.validation["outcome"] == tier3.result.validation["outcome"] else 1
