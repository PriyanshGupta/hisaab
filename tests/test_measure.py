"""The fixture measurement behind PRD §9 (`python -m hisaab measure-fixtures`), on synthetic fixtures."""

import json
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from hisaab.parsing.measure import FixtureResult, Summary, load_seed_templates, measure, summarize
from tests.pdf_factory import make_pdf
from tests.statement_samples import ruled_page
from tests.test_email_interpreter import BODY, NOTICE_BODY
from tests.test_email_spec import NOTICE, SPEC
from tests.test_statement_interpreter import ROWS
from tests.test_statement_spec import RULED

GOOD: dict[str, Any] = {"outcome": "transaction", "amount": 123450, "currency": "INR", "direction": "debit",
                        "account_identifier": "1111", "counterparty_identifier": "shop.owner@okbank",
                        "payee_raw": "Corner Shop", "reference_number": "9988776655", "date": "2026-10-05",
                        "time": None, "balance": None, "missing_fields": []}


def write_eml(path: Path, body: str, subject: str = "Debit alert") -> None:
    msg: EmailMessage = EmailMessage()
    msg["From"], msg["Subject"], msg["Date"] = "alerts@example-bank.in", subject, "Mon, 05 Oct 2026 10:00:00 +0000"
    msg.set_content(body, subtype="html")
    path.write_bytes(msg.as_bytes())


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    emails: Path = tmp_path / "emails" / "x"
    emails.mkdir(parents=True)
    write_eml(emails / "bank_debit_01.eml", BODY)
    (emails / "bank_debit_01.expected.json").write_text(json.dumps(GOOD))
    write_eml(emails / "bank_debit_02.eml", BODY.replace("1,234.50", "1,234.00"))  # the parser is right, ...
    (emails / "bank_debit_02.expected.json").write_text(json.dumps(GOOD))  # ... the expected file says 1,234.50
    write_eml(emails / "bank_notice_01.eml", NOTICE_BODY, subject="Account update")
    (emails / "bank_notice_01.expected.json").write_text(json.dumps({**dict.fromkeys(GOOD), "outcome": "non_transactional",
                                                                     "missing_fields": []}))
    write_eml(emails / "no_expected_01.eml", BODY)  # no expected file: not measured
    statements: Path = tmp_path / "statements"
    statements.mkdir()
    page: dict[str, Any] = ruled_page(ROWS, first=True, summary=["STATEMENT SUMMARY", "Opening 1,000.00", "Closing 1,450.00"])
    (statements / "bank_savings_01.pdf").write_bytes(make_pdf([page]))
    (statements / "bank_savings_01.expected.json").write_text(json.dumps({
        "account": ["last4", "1234"],
        "rows": [["2026-07-01", "debit", 4000], ["2026-07-02", "credit", 50000], ["2026-07-03", "debit", 999]],
    }))
    seeds: Path = tmp_path / "seeds"
    seeds.mkdir()
    (seeds / "bank_debit.json").write_text(json.dumps(SPEC))
    (seeds / "bank_notice.json").write_text(json.dumps({**NOTICE, "match": {**NOTICE["match"], "subject": "^Account update"}}))
    (seeds / "bank_savings.json").write_text(json.dumps(RULED))
    return tmp_path


def run(corpus: Path, held_out: frozenset[str] = frozenset()) -> list[FixtureResult]:
    return measure(corpus, load_seed_templates(corpus / "seeds"), held_out)


def test_each_measured_fixture_reports_its_transactions_and_how_many_were_right(corpus: Path) -> None:
    by_name: dict[str, FixtureResult] = {r.name: r for r in run(corpus)}
    assert set(by_name) == {"bank_debit_01", "bank_debit_02", "bank_notice_01", "bank_savings_01"}
    assert (by_name["bank_debit_01"].transactions, by_name["bank_debit_01"].correct) == (1, 1)
    assert (by_name["bank_debit_02"].transactions, by_name["bank_debit_02"].correct) == (1, 0)
    assert "amount" in by_name["bank_debit_02"].detail
    assert (by_name["bank_savings_01"].transactions, by_name["bank_savings_01"].correct) == (3, 2)
    assert by_name["bank_savings_01"].template == "bank_savings"


def test_a_notice_is_judged_on_being_classified_not_counted_as_a_transaction(corpus: Path) -> None:
    notice: FixtureResult = next(r for r in run(corpus) if r.name == "bank_notice_01")
    assert (notice.kind, notice.transactions, notice.ok) == ("notice", 0, True)


def test_a_field_outside_the_criterion_is_reported_but_does_not_fail_the_transaction(corpus: Path) -> None:
    wrong_payee: dict[str, Any] = {**GOOD, "payee_raw": "Someone Else"}
    (corpus / "emails" / "x" / "bank_debit_01.expected.json").write_text(json.dumps(wrong_payee))
    first: FixtureResult = next(r for r in run(corpus) if r.name == "bank_debit_01")
    assert first.correct == 1 and "payee_raw" in first.detail


def test_the_summary_is_the_prd_9_rate_over_transactions(corpus: Path) -> None:
    summary: Summary = summarize(run(corpus, held_out=frozenset({"bank_debit_02"})))
    assert (summary.transactions, summary.correct) == (5, 3)  # 1 + 1 + 3 rows
    assert summary.rate == pytest.approx(0.6)
    assert (summary.held_out_transactions, summary.held_out_correct) == (1, 0)


def test_an_unmatched_email_counts_as_wrong_and_says_tier_2(corpus: Path) -> None:
    (corpus / "seeds" / "bank_debit.json").unlink()
    first: FixtureResult = next(r for r in run(corpus) if r.name == "bank_debit_01")
    assert first.correct == 0 and "tier 2" in first.detail


def test_a_statement_bound_to_the_wrong_account_gets_no_row_right(corpus: Path) -> None:
    path: Path = corpus / "statements" / "bank_savings_01.expected.json"
    expected: dict[str, Any] = json.loads(path.read_text())
    path.write_text(json.dumps({**expected, "account": ["last4", "9999"]}))
    savings: FixtureResult = next(r for r in run(corpus) if r.name == "bank_savings_01")
    assert savings.correct == 0 and "account: got ['last4', '1234']" in savings.detail
