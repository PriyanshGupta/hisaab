"""The two HDFC statement seed specs against the real fixture PDFs (build-plan slice 10).

Nothing from the fixtures is written here. Every expected figure is read at
runtime from the statement's own printed summary (counts, totals, the account
number) by a simple pattern independent of the spec, and the encrypted copy's
password comes from HISAAB_TEST_PDF_PASSWORD.
"""

import copy
import json
import os
import re
from importlib.resources import files
from pathlib import Path
from re import Match
from typing import Any

import pytest

from hisaab.config import PROJECT_ROOT
from hisaab.parsing.pdf import Page, PasswordRequired, read_pdf
from hisaab.parsing.statement_interpreter import StatementResult, StatementRow, detect, interpret_statement
from hisaab.parsing.statement_spec import parse_statement_spec
from hisaab.parsing.transforms import inr_to_paise
from tests.pdf_corrupt import replace_drawn_text

STATEMENTS: Path = PROJECT_ROOT / "fixtures" / "statements"
pytestmark: pytest.MarkDecorator = pytest.mark.skipif(not STATEMENTS.is_dir(), reason="fixtures are local-only (gitignored)")
AMOUNT: str = r"[\d,]+\.\d{2}"


def seed(name: str) -> dict[str, Any]:
    spec: dict[str, Any] = json.loads((files("hisaab.parsing") / "seed" / f"{name}.json").read_text())
    return spec


def _find(pattern: str, text: str) -> Match[str]:
    """re.search for text the statement is known to print."""
    found: Match[str] | None = re.search(pattern, text)
    assert found is not None, pattern
    return found


def pdf_bytes(name: str) -> bytes:
    return (STATEMENTS / f"{name}.pdf").read_bytes()


def parse(spec_name: str, data: bytes, spec: dict[str, Any] | None = None) -> tuple[list[Page], StatementResult]:
    pages: list[Page] = read_pdf(data)
    return pages, interpret_statement(parse_statement_spec(spec or seed(spec_name)), pages)


# --- savings ------------------------------------------------------------------------------

def savings_summary(pages: list[Page]) -> tuple[int, int, int, int, int, int]:
    """The statement's own STATEMENT SUMMARY row: opening, Dr count, Cr count, debits, credits, closing."""
    text: str = "\n".join(p.text for p in pages)
    m: Match[str] = _find(rf"ClosingBal\n({AMOUNT}) (\d+) (\d+) ({AMOUNT}) ({AMOUNT}) ({AMOUNT})", text)
    opening, dr, cr, debits, credits, closing = m.groups()
    return inr_to_paise(opening), int(dr), int(cr), inr_to_paise(debits), inr_to_paise(credits), inr_to_paise(closing)


def test_savings_rows_match_the_statements_own_counts_and_totals() -> None:
    pages, result = parse("hdfc_acct_statement", pdf_bytes("hdfc_acct_statement_01"))
    opening, dr, cr, debits, credits, closing = savings_summary(pages)
    debit_rows: list[StatementRow] = [r for r in result.rows if r.direction == "debit"]
    credit_rows: list[StatementRow] = [r for r in result.rows if r.direction == "credit"]
    assert (len(debit_rows), len(credit_rows)) == (dr, cr)
    assert sum(r.amount for r in debit_rows) == debits and sum(r.amount for r in credit_rows) == credits
    assert result.unparseable == []
    v: dict[str, Any] = result.validation
    assert (v["outcome"], v["gap_paise"]) == ("passed", 0)
    assert v["declared"] == {"opening": opening, "closing": closing}


def test_every_savings_row_amount_and_direction_agrees_with_the_running_balance() -> None:
    # Stronger than totals: each row's amount and direction, checked against the bank's
    # running balance column (a test-only cross-check; validation never uses that column).
    pages, result = parse("hdfc_acct_statement", pdf_bytes("hdfc_acct_statement_01"))
    balance: int = savings_summary(pages)[0]
    for row in result.rows:
        balance += row.amount if row.direction == "credit" else -row.amount
        assert row.balance == balance, row


def test_the_savings_account_binds_to_the_last_four_of_the_printed_account_number() -> None:
    pages, result = parse("hdfc_acct_statement", pdf_bytes("hdfc_acct_statement_01"))
    number: str = _find(r"AccountNo\s*:\s*(\d+)", pages[0].text).group(1)
    assert result.account == ("last4", number[-4:])


def test_narrations_wrapped_across_a_page_break_are_rejoined() -> None:
    pages, result = parse("hdfc_acct_statement", pdf_bytes("hdfc_acct_statement_01"))
    # the first line on page 2's table continues the last row of page 1
    tail: str = next(ln.text for ln in pages[1].lines if re.search("account$", ln.text))
    first_continuation: str = pages[1].lines[[ln.text for ln in pages[1].lines].index(tail) + 1].text
    last_on_page1: StatementRow = [r for r in result.rows if r.page == 1][-1]
    assert first_continuation in last_on_page1.narration
    assert not re.match(r"\d{2}/\d{2}/\d{2}", first_continuation)  # it really was a continuation line


def test_a_corrupted_amount_fails_validation_with_exactly_the_gap_it_introduced() -> None:
    data: bytes = pdf_bytes("hdfc_acct_statement_01")
    _, clean = parse("hdfc_acct_statement", data)
    for row in (r for r in clean.rows if r.direction == "debit" and r.amount >= 10_000):
        rupees: int = row.amount // 100
        text: str = f"{rupees:,}.{row.amount % 100:02d}"
        tens: int = len(text) - 5  # the tens-of-rupees digit, e.g. "1,240.72" -> "4"
        if text[tens] == "9":
            continue
        corrupted: str = text[:tens] + str(int(text[tens]) + 1) + text[tens + 1:]  # ₹10 more
        try:
            bad: bytes = replace_drawn_text(data, text, corrupted)
        except ValueError:  # drawn more than once; try another row
            continue
        break
    else:
        pytest.fail("no uniquely drawn debit amount to corrupt")
    _, result = parse("hdfc_acct_statement", bad)
    v: dict[str, Any] = result.validation
    assert v["outcome"] == "failed"
    assert v["gap_paise"] == 1000  # debits up ₹10, so the rows come to ₹10 less than the declared closing


def test_a_layout_with_no_declared_balances_is_unavailable_not_a_false_pass() -> None:
    spec: dict[str, Any] = copy.deepcopy(seed("hdfc_acct_statement"))
    del spec["declared_balances"]  # the running balance column is still there; it must not be used
    _, result = parse("hdfc_acct_statement", pdf_bytes("hdfc_acct_statement_01"), spec)
    assert result.validation["outcome"] == "unavailable"
    assert result.validation["gap_paise"] is None and len(result.rows) > 0


def test_the_encrypted_copy_needs_its_password_and_then_reads_identically() -> None:
    encrypted: bytes = pdf_bytes("hdfc_acct_statement_01_encrypted")
    with pytest.raises(PasswordRequired):
        read_pdf(encrypted)
    password: str | None = os.environ.get("HISAAB_TEST_PDF_PASSWORD")
    if not password:
        pytest.skip("set HISAAB_TEST_PDF_PASSWORD to the encrypted fixture's password")
    plain: list[Page] = read_pdf(pdf_bytes("hdfc_acct_statement_01"))
    assert [p.text for p in read_pdf(encrypted, password)] == [p.text for p in plain]


# --- card ---------------------------------------------------------------------------------

def card_summary(pages: list[Page]) -> list[int]:
    """Previous dues, payments/credits, purchases/debits, finance charges, total due as printed."""
    text: str = pages[0].text
    prev, paid, bought, charges = _find(rf"₹ ({AMOUNT}) ₹ ({AMOUNT}) \+ ₹ ({AMOUNT}) \+ ₹ ({AMOUNT}) =", text).groups()
    total: str = _find(rf"_ ₹ ({AMOUNT})", text).group(1)
    return [inr_to_paise(x) for x in (prev, paid, bought, charges, total)]


def test_card_rows_match_the_printed_purchases_and_payments() -> None:
    pages, result = parse("hdfc_cc_statement", pdf_bytes("hdfc_cc_statement_01"))
    prev, paid, bought, charges, total = card_summary(pages)
    dated_lines: int = sum(1 for p in pages for ln in p.lines if re.match(r"\d{2}/\d{2}/\d{4}\s*\|", ln.text))
    assert len(result.rows) == dated_lines and result.unparseable == []
    assert sum(r.amount for r in result.rows if r.direction == "debit") == bought + charges
    assert sum(r.amount for r in result.rows if r.direction == "credit") == paid
    v: dict[str, Any] = result.validation
    assert v["outcome"] == "passed" and abs(v["gap_paise"]) <= 100
    assert v["declared"] == {"opening": prev, "closing": total}


def test_the_card_binds_to_the_last_four_of_the_printed_card_number() -> None:
    pages, result = parse("hdfc_cc_statement", pdf_bytes("hdfc_cc_statement_01"))
    number: str = _find(r"Credit Card No\.\s*(\S+)", pages[0].text).group(1)
    assert result.account == ("card_last4", number[-4:])


def test_each_spec_detects_only_its_own_statement() -> None:
    savings, card = read_pdf(pdf_bytes("hdfc_acct_statement_01")), read_pdf(pdf_bytes("hdfc_cc_statement_01"))
    acct_spec, cc_spec = parse_statement_spec(seed("hdfc_acct_statement")), parse_statement_spec(seed("hdfc_cc_statement"))
    assert detect(acct_spec, savings) and not detect(acct_spec, card)
    assert detect(cc_spec, card) and not detect(cc_spec, savings)
