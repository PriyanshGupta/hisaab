"""PRD §9 over the real fixture corpus: every format parses, and the rate stays at 95%+ (slice 11).

Fixtures and their hand-checked answers are local-only (gitignored); this skips without them.
"""

from pathlib import Path

import pytest

from hisaab.cli import HELD_OUT
from hisaab.config import PROJECT_ROOT
from hisaab.parsing.measure import FixtureResult, Summary, load_seed_templates, measure, summarize
from hisaab.seeds import SEED_TEMPLATES

FIXTURES: Path = PROJECT_ROOT / "fixtures"
pytestmark: pytest.MarkDecorator = pytest.mark.skipif(not FIXTURES.is_dir(), reason="fixtures are local-only")
# The SBI card statement prints only the last two card digits, so it cannot bind to an account; the upload
# preview asks which account it is (tech-design §4.7). Strictly counted as "account not correct".
KNOWN_UNBINDABLE: set[str] = {"sbi_cc_statement_01"}


@pytest.fixture(scope="module")
def results() -> list[FixtureResult]:
    return measure(FIXTURES, load_seed_templates(SEED_TEMPLATES), HELD_OUT)


def test_every_fixture_format_is_measured(results: list[FixtureResult]) -> None:
    assert {r.format for r in results} >= {
        "hdfc_upi_debit", "hdfc_upi_credit", "hdfc_cc_purchase", "icici_cc_purchase", "sbi_cc_purchase",
        "scapia_cc_purchase", "icici_cc_repayment", "scapia_cc_repayment", "hdfc_cc_mandate", "hdfc_cc_mandate_paid",
        "hdfc_acct_statement", "hdfc_cc_statement", "icici_cc_statement", "sbi_cc_statement", "scapia_cc_statement",
    }


def test_every_fixture_parses_correctly(results: list[FixtureResult]) -> None:
    failing: list[str] = [f"{r.name}: {r.detail}" for r in results if not r.ok and r.name not in KNOWN_UNBINDABLE]
    assert failing == []


def test_the_unbindable_statement_is_wrong_only_on_its_account(results: list[FixtureResult]) -> None:
    sbi: FixtureResult = next(r for r in results if r.name == "sbi_cc_statement_01")
    assert sbi.detail.startswith("account: got None") and "rows wrong" not in sbi.detail


def test_the_prd_9_rate_is_at_least_95_percent(results: list[FixtureResult]) -> None:
    summary: Summary = summarize(results)
    assert summary.rate >= 0.95, f"{summary.correct}/{summary.transactions}"
    assert summary.held_out_correct == summary.held_out_transactions


def test_no_field_outside_the_criterion_is_wrong_either(results: list[FixtureResult]) -> None:
    emails: list[str] = [f"{r.name}: {r.detail}" for r in results if r.kind == "email" and r.detail]
    assert emails == []
