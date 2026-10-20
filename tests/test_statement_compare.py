"""compare-tiers: one statement read by its template and by the model, and where they differ (ruling 2026-10-19).

The live check for tier 3. It runs on the owner's own downloaded statements only: fixtures are unredacted and
never go to an LLM (CLAUDE.md), so a path under fixtures/ is refused before anything is read.
"""

import copy
import io
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from hisaab.config import Settings
from hisaab.db import Database
from hisaab.parsing import templates
from hisaab.statements.compare import compare_tiers
from tests.claude_fake import FakeClaude
from tests.statement_samples import ruled_page, ruled_pdf
from tests.test_statement_spec import RULED

ROWS: list[tuple[str, ...]] = [("01/09/26", "UPI-TEA STALL", "", "100.00", "", "900.00"),
                               ("02/09/26", "SALARY", "", "", "500.00", "1,400.00")]
PDF: bytes = ruled_pdf(ruled_page(ROWS, first=True, summary=["STATEMENT SUMMARY", "Opening 1,000.00", "Closing 1,400.00"]))
ROW: dict[str, Any] = {"page": 1, "date": "2026-09-01", "time": None, "narration": "UPI-TEA STALL", "reference": None,
                       "direction": "debit", "amount": 10000, "balance": 90000, "bill_payment": False}
AGREES: dict[str, Any] = {
    "outcome": "statement", "account_kind": "bank", "account_last4": "1234", "institution": "Example Bank",
    "opening": 100000, "closing": 140000, "unreadable": [],
    "rows": [ROW, {**ROW, "date": "2026-09-02", "narration": "SALARY", "direction": "credit", "amount": 50000,
                   "balance": 140000}],
}


@pytest.fixture
def setup(db: Database, settings: Settings, tmp_path: Path) -> tuple[Database, Settings, Path]:
    with db.write_session() as s:
        templates.save_statement_template(s, name="Example statement", spec=RULED, status="active")
    pdf: Path = tmp_path / "downloads" / "sept.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(PDF)
    return db, settings.model_copy(update={"anthropic_api_key": SecretStr("sk-test")}), pdf


def run(setup: tuple[Database, Settings, Path], answer: dict[str, Any], **kw: Any) -> tuple[int, str, FakeClaude]:
    db, settings, pdf = setup
    claude: FakeClaude = FakeClaude(reply=lambda prompt: answer)
    out: io.StringIO = io.StringIO()
    code: int = compare_tiers(db, settings, kw.pop("pdf", pdf), None, out, transport=claude, sleep=lambda s: None, **kw)
    return code, out.getvalue(), claude


def test_agreeing_tiers_exit_zero(setup: tuple[Database, Settings, Path]) -> None:
    code, out, claude = run(setup, AGREES)
    assert code == 0 and claude.calls == 1
    assert "Template “Example statement”: 2 rows, check passed (gap ₹0.00)" in out
    assert "Claude: 2 rows, check passed (gap ₹0.00)" in out
    assert "Same 2 rows in both." in out


def test_a_row_the_model_missed_is_named_and_exits_one(setup: tuple[Database, Settings, Path]) -> None:
    answer: dict[str, Any] = copy.deepcopy(AGREES)
    answer["rows"] = answer["rows"][:1]
    code, out, _ = run(setup, answer)
    assert code == 1
    assert "Only the template has: 2026-09-02 credit ₹500.00" in out
    assert "Claude: 1 row, check failed" in out


def test_narration_differences_are_counted_not_failed(setup: tuple[Database, Settings, Path]) -> None:
    answer: dict[str, Any] = copy.deepcopy(AGREES)
    answer["rows"][0]["narration"] = "UPI TEA STALL"
    code, out, _ = run(setup, answer)
    assert code == 0 and "1 row's narration differs." in out


def test_fixtures_never_go_to_the_model(setup: tuple[Database, Settings, Path], tmp_path: Path) -> None:
    fixture: Path = tmp_path / "fixtures" / "statements" / "x.pdf"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(PDF)
    code, out, claude = run(setup, AGREES, pdf=fixture, fixtures_dir=tmp_path / "fixtures")
    assert code == 2 and claude.calls == 0
    assert "Fixtures are unredacted and never go to an LLM" in out


def test_a_statement_no_template_reads_has_nothing_to_compare(setup: tuple[Database, Settings, Path],
                                                              tmp_path: Path) -> None:
    from tests.test_statement_reading import UNKNOWN

    other: Path = tmp_path / "downloads" / "other.pdf"
    other.write_bytes(UNKNOWN)
    code, out, claude = run(setup, AGREES, pdf=other)
    assert code == 2 and claude.calls == 0 and "No known layout reads this statement" in out
