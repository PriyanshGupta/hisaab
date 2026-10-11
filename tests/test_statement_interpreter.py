"""The statement interpreter (tech-design §4.7) on synthetic statements."""

import copy

import pytest

from hisaab.parsing.pdf import read_pdf
from hisaab.parsing.statement_interpreter import (
    RowError,
    TemplateMismatch,
    amount_with_direction,
    detect,
    interpret_statement,
)
from hisaab.parsing.statement_spec import parse_statement_spec
from tests.pdf_factory import make_pdf
from tests.statement_samples import LABELS, card_page, ruled_page
from tests.test_statement_spec import LINES, RULED

ROWS = [
    ("01/07/26", "UPI-TEA STALL", "0001", "40.00", "", "960.00"),
    ("02/07/26", "SALARY JULY", "0002", "", "500.00", "1,460.00"),
    ("03/07/26", "UPI-BOOKS", "0003", "10.00", "", "1,450.00"),
]
SUMMARY = ["STATEMENT SUMMARY", "Opening 1,000.00", "Closing 1,450.00"]


def run(spec, *pages, **kw):
    return interpret_statement(parse_statement_spec(spec), read_pdf(make_pdf(list(pages))), **kw)


def simple(result):
    return [(r.date, r.narration, r.reference, r.direction, r.amount, r.balance) for r in result.rows]


# --- ruled rows ----------------------------------------------------------------------------

def test_ruled_rows_map_cells_by_column_and_convert_them():
    result = run(RULED, ruled_page(ROWS, first=True, summary=SUMMARY))
    assert simple(result) == [
        ("2026-07-01", "UPI-TEA STALL", "0001", "debit", 4000, 96000),
        ("2026-07-02", "SALARY JULY", "0002", "credit", 50000, 146000),
        ("2026-07-03", "UPI-BOOKS", "0003", "debit", 1000, 145000),
    ]
    assert result.account == ("last4", "1234")
    assert result.unparseable == [] and result.pages_processed == 1


def test_a_row_without_a_date_continues_the_narration_above_even_across_a_page_break():
    page1 = ruled_page([ROWS[0], ("", "-VPA-TEASTALL@YBL", "", "", "", "")], first=True)
    page2 = ruled_page([("", "-UPI", "", "", "", ""), ROWS[1]], summary=SUMMARY)
    result = run(RULED, page1, page2)
    assert [r.narration for r in result.rows] == ["UPI-TEA STALL-VPA-TEASTALL@YBL-UPI", "SALARY JULY"]
    assert result.pages_processed == 2


def test_the_page_footer_after_ends_at_is_not_folded_into_the_last_row():
    result = run(RULED, ruled_page(ROWS[:1], first=True))
    assert result.rows[0].narration == "UPI-TEA STALL"


def test_skip_matching_rows_are_dropped_and_counted():
    result = run(RULED, ruled_page([("", "Brought forward", "", "", "", "1,000.00"), *ROWS], first=True))
    assert len(result.rows) == 3 and result.rows_skipped == 1


def test_stop_at_ends_the_statement():
    page = ruled_page([ROWS[0]], first=True, summary=["STATEMENT SUMMARY"])
    page["texts"].append((33, 400, "09/07/26"))  # a date-looking line after the summary
    result = run(RULED, page)
    assert len(result.rows) == 1


def test_a_row_with_both_or_neither_amount_is_unparseable_and_kept_for_the_diagnostic():
    both = ("04/07/26", "ODD", "0004", "5.00", "5.00", "")
    neither = ("05/07/26", "EMPTY", "0005", "", "", "")
    result = run(RULED, ruled_page([ROWS[0], both, neither], first=True))
    assert len(result.rows) == 1
    assert [(u["page"], u["raw_cells"][1], u["reason"]) for u in result.unparseable] == [
        (1, "ODD", "both debit and credit are filled"), (1, "EMPTY", "neither debit nor credit is filled")]


def test_a_bad_date_makes_the_row_unparseable():
    result = run(RULED, ruled_page([("31/02/26", "X", "1", "1.00", "", "")], first=True))
    assert result.rows == [] and result.unparseable[0]["reason"] == "date: not a date in the form %d/%m/%y"


def test_a_continuation_line_carrying_an_amount_is_unparseable_not_merged():
    result = run(RULED, ruled_page([ROWS[0], ("", "MORE", "", "9.00", "", "")], first=True))
    assert len(result.rows) == 1 and result.unparseable[0]["raw_cells"][1] == "MORE"


def test_continuation_none_makes_dateless_rows_unparseable():
    spec = copy.deepcopy(RULED)
    spec["row_rules"]["continuation"] = "none"
    result = run(spec, ruled_page([ROWS[0], ("", "MORE", "", "", "", "")], first=True))
    assert result.rows[0].narration == "UPI-TEA STALL" and len(result.unparseable) == 1


def test_reordered_columns_fail_the_header_sanity_check():
    swapped = LABELS[:3] + ["Deposit", "Withdrawal"] + LABELS[5:]  # a bank swapping its amount columns
    with pytest.raises(TemplateMismatch, match="Withdrawal"):
        run(RULED, ruled_page(ROWS, first=True, labels=swapped))


def test_a_different_number_of_columns_fails():
    page = ruled_page(ROWS, first=True)
    page["vlines"] = page["vlines"][:-1]  # the right border is gone: 5 columns
    with pytest.raises(TemplateMismatch, match="5 columns"):
        run(RULED, page)


def test_no_header_anywhere_fails():
    with pytest.raises(TemplateMismatch, match="header"):
        run(RULED, {"texts": [(40, 30, "Example Bank"), (40, 45, "Statement of account")]})


# --- line_pattern rows -------------------------------------------------------------------------

CARD_LINES = [
    ("17/07/2026| 22:11 ZOMATO GURGAON", "1,885.07", False),
    ("NAME SURNAME [CKYC ID : 1 ]", None, False),
    ("18/07/2026| 00:28 CREDIT CARD PAYMENT", "54,295.00", True),
]


def test_line_pattern_rows_use_named_groups_and_the_plus_sign_marks_credits():
    result = run(LINES, card_page(CARD_LINES, first=True))
    assert [(r.date, r.time, r.narration, r.direction, r.amount) for r in result.rows] == [
        ("2026-07-17", "22:11:00", "ZOMATO GURGAON", "debit", 188507),
        ("2026-07-18", "00:28:00", "CREDIT CARD PAYMENT", "credit", 5429500),
    ]
    assert result.rows_skipped == 1 and result.account == ("card_last4", "5678")


def test_an_unmatched_line_in_a_line_pattern_table_is_unparseable_when_there_is_no_continuation():
    result = run(LINES, card_page([*CARD_LINES, ("SOMETHING ELSE ENTIRELY", None, False)], first=True))
    assert [u["raw_cells"] for u in result.unparseable] == [["SOMETHING ELSE ENTIRELY"]]


def test_an_unmatched_line_continues_the_row_above_with_empty_date_column():
    spec = copy.deepcopy(LINES)
    spec["row_rules"]["continuation"] = "empty_date_column"
    result = run(spec, card_page([CARD_LINES[0], ("BANGALORE", None, False)], first=True))
    assert result.rows[0].narration == "ZOMATO GURGAONBANGALORE" and result.unparseable == []


# --- detection, binding ------------------------------------------------------------------------

def test_detect_needs_every_all_of_phrase_and_no_none_of_phrase():
    spec = parse_statement_spec(RULED)
    assert detect(spec, read_pdf(make_pdf([ruled_page(ROWS, first=True)])))
    assert not detect(spec, read_pdf(make_pdf([card_page(CARD_LINES, first=True)])))
    excluded = copy.deepcopy(RULED)
    excluded["detect"]["none_of"] = ["Account No"]
    assert not detect(parse_statement_spec(excluded), read_pdf(make_pdf([ruled_page(ROWS, first=True)])))


def test_an_unbound_account_is_none_not_an_error():
    page = ruled_page(ROWS, first=True)
    page["texts"] = [t for t in page["texts"] if not t[2].startswith("Account No")]
    assert run(RULED, page).account is None  # the preview will ask which account (§4.7)


# --- the three amount styles --------------------------------------------------------------------

@pytest.mark.parametrize(("cells", "out"), [
    ({"debit": "40.00", "credit": ""}, ("debit", "40.00")),
    ({"debit": "", "credit": "1,250.00"}, ("credit", "1,250.00")),
])
def test_separate_debit_credit(cells, out):
    assert amount_with_direction("separate_debit_credit", cells, None) == out


@pytest.mark.parametrize(("text", "unsigned", "out"), [
    ("+ ₹ 168.31", "debit", ("credit", "₹ 168.31")),
    ("₹ 1,885.07", "debit", ("debit", "₹ 1,885.07")),
    ("-1,250.00", "credit", ("debit", "1,250.00")),
    ("1,250.00", "credit", ("credit", "1,250.00")),
])
def test_signed_single_column(text, unsigned, out):
    assert amount_with_direction("signed_single_column", {"amount": text}, unsigned) == out


@pytest.mark.parametrize(("text", "out"), [
    ("2,050.00 Cr", ("credit", "2,050.00")), ("2,050.00 C", ("credit", "2,050.00")),
    ("2,050.00 Dr", ("debit", "2,050.00")), ("99.00 D", ("debit", "99.00")), ("5.00", ("debit", "5.00")),
])
def test_cr_dr_suffix(text, out):
    assert amount_with_direction("cr_dr_suffix", {"amount": text}, "debit") == out


def test_an_empty_single_amount_is_a_row_error():
    with pytest.raises(RowError):
        amount_with_direction("signed_single_column", {"amount": ""}, "debit")


# --- tables that carry on across pages without repeating their header ----------------------

def continued_page(rows):
    """A later page: the page header block, then rows with no header row (HDFC savings does this)."""
    page = ruled_page(rows, labels=["", "", "", "", "", ""])
    page["texts"] = [t for t in page["texts"] if t[2]] + [(40, 60, "From : 01/07/2026 To : 31/07/2026 Statement")]
    return page


def test_a_table_resumes_after_the_page_header_on_pages_without_a_header_row():
    spec = copy.deepcopy(RULED)
    spec["table"]["resumes_after"] = "^From : .* Statement$"
    result = run(spec, ruled_page(ROWS[:1], first=True), continued_page(ROWS[1:]),
                 continued_page([("", "", "", "", "", "")]) | {"texts": [(40, 60, "From : x Statement"), *[
                     (40, 115 + 15 * i, line) for i, line in enumerate(["STATEMENT SUMMARY", "Opening 1,000.00",
                                                                        "Closing 1,450.00"])]]})
    assert [r.narration for r in result.rows] == ["UPI-TEA STALL", "SALARY JULY", "UPI-BOOKS"]
    assert result.validation["outcome"] == "passed"


def test_without_resumes_after_a_headerless_page_contributes_nothing():
    result = run(RULED, ruled_page(ROWS[:1], first=True), continued_page(ROWS[1:]))
    assert len(result.rows) == 1


def test_a_zero_amount_row_is_unparseable():
    result = run(RULED, ruled_page([ROWS[0], ("02/07/26", "REVERSAL", "9", "0.00", "", "")], first=True))
    assert len(result.rows) == 1 and result.unparseable[0]["reason"] == "amount is zero"
