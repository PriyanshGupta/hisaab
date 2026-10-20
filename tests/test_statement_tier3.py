"""Tier 3 (tech-design §4.7, §4.8; ruling 2026-10-19): an unknown layout read by the model, then held to the PDF.

Every row's amount must be printed on the page the model says it is on, and the declared balances must be
printed somewhere in the statement; otherwise the row can't be read, and the check is unavailable. The
check itself is the same §4.8 arithmetic as for a template.
"""

import copy
from typing import Any

import pytest
from pydantic import SecretStr

from hisaab.config import Settings
from hisaab.parsing.pdf import Line, Page, Word, layout_text, read_pdf
from hisaab.statements.tier3 import ModelRead, NotAStatement, read_with_model
from tests.claude_fake import FakeClaude
from tests.pdf_factory import make_pdf
from tests.statement_samples import ruled_page, ruled_pdf

ROWS: list[tuple[str, ...]] = [("01/09/26", "UPI-TEA STALL", "", "100.00", "", "900.00"),
                               ("02/09/26", "SALARY", "", "", "500.00", "1,400.00")]
PAGES: list[Page] = read_pdf(ruled_pdf(ruled_page(ROWS, first=True,
                                                  summary=["Opening 1,000.00", "Closing 1,400.00"])))
ROW: dict[str, Any] = {"page": 1, "date": "2026-09-01", "time": None, "narration": "UPI-TEA STALL", "reference": None,
                       "direction": "debit", "amount": 10000, "balance": 90000, "bill_payment": False}
ANSWER: dict[str, Any] = {
    "outcome": "statement", "account_kind": "bank", "account_last4": "1234", "institution": "Example Bank",
    "opening": 100000, "closing": 140000,
    "rows": [ROW, {**ROW, "date": "2026-09-02", "narration": "SALARY", "direction": "credit", "amount": 50000,
                   "balance": 140000}],
    "unreadable": [],
}


@pytest.fixture
def llm_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"anthropic_api_key": SecretStr("sk-test")})


def read(settings: Settings, answer: dict[str, Any], pages: list[Page] = PAGES, **kw: Any) -> ModelRead:
    claude: FakeClaude = FakeClaude(reply=lambda prompt: answer)
    return read_with_model(settings, pages, transport=claude, sleep=lambda s: None, **kw)


def changed(**kw: Any) -> dict[str, Any]:
    return copy.deepcopy(ANSWER) | kw


def test_rows_printed_on_their_page_are_kept_and_checked(llm_settings: Settings) -> None:
    got: ModelRead = read(llm_settings, ANSWER)
    assert [(r.date, r.direction, r.amount, r.narration) for r in got.result.rows] == [
        ("2026-09-01", "debit", 10000, "UPI-TEA STALL"), ("2026-09-02", "credit", 50000, "SALARY")]
    assert (got.account_kind, got.account) == ("bank", ("last4", "1234"))
    v: dict[str, Any] = got.result.validation
    assert (v["outcome"], v["tier"], v["model"], v["institution"], v["template_name"]) == (
        "passed", 3, "claude-sonnet-5-5", "Example Bank", None)
    assert v["declared"] == {"opening": 100000, "closing": 140000} and got.result.unparseable == []


def test_a_row_whose_amount_is_not_printed_on_its_page_cannot_be_read(llm_settings: Settings) -> None:
    invented: dict[str, Any] = {**ROW, "date": "2026-09-03", "narration": "MADE UP", "amount": 12345}
    got: ModelRead = read(llm_settings, changed(rows=[*ANSWER["rows"], invented]))
    assert len(got.result.rows) == 2
    [bad] = got.result.unparseable
    assert bad["page"] == 1 and "isn't printed on page 1" in bad["reason"]
    assert bad["raw_cells"] == ["2026-09-03", "MADE UP", "123.45"]


def test_declared_balances_not_printed_make_the_check_unavailable(llm_settings: Settings) -> None:
    got: ModelRead = read(llm_settings, changed(opening=100000, closing=150000))  # 1,500.00 appears nowhere
    v: dict[str, Any] = got.result.validation
    # kept only as a pair: a lone "closing" could be any printed figure, and would become a balance observation
    assert v["outcome"] == "unavailable" and v["declared"] == {"opening": None, "closing": None}
    assert v["declared_dropped"] == ["opening", "closing"]


def test_rows_the_model_could_not_read_and_malformed_rows_are_unreadable(llm_settings: Settings) -> None:
    got: ModelRead = read(llm_settings, changed(
        rows=[*ANSWER["rows"], {**ROW, "page": 7}, {**ROW, "date": "2026-02-30"}],
        unreadable=[{"page": 1, "text": "03/09/26 ??? 7.00", "reason": "amount is smudged"}]))
    assert len(got.result.rows) == 2
    reasons: list[str] = [u["reason"] for u in got.result.unparseable]
    assert "amount is smudged" in reasons and any("page 7" in r for r in reasons) and any("2026-02-30" in r for r in reasons)
    assert got.result.validation["rows_unparseable"] == 3


def test_bill_payment_counts_only_on_card_statements(llm_settings: Settings) -> None:
    paid: list[dict[str, Any]] = [{**r, "bill_payment": True} for r in ANSWER["rows"]]
    assert [r.payment for r in read(llm_settings, changed(rows=paid)).result.rows] == [False, False]
    card: ModelRead = read(llm_settings, changed(rows=paid, account_kind="credit_card"))
    assert [r.payment for r in card.result.rows] == [False, True]  # only a credit pays a bill
    assert card.account == ("card_last4", "1234")


def test_not_a_statement_is_refused(llm_settings: Settings) -> None:
    with pytest.raises(NotAStatement):
        read(llm_settings, changed(outcome="not_a_statement", rows=[]))
    with pytest.raises(NotAStatement):
        read(llm_settings, changed(account_kind=None))


def test_a_template_that_was_tried_first_is_recorded(llm_settings: Settings) -> None:
    got: ModelRead = read(llm_settings, ANSWER, tried={"template": "Example statement", "reason": "a column moved"})
    assert got.result.validation["template_tried"] == {"template": "Example statement", "reason": "a column moved"}


def test_layout_text_keeps_debit_and_credit_columns_apart() -> None:
    lines: list[str] = layout_text(PAGES[0]).splitlines()
    debit: str = next(line for line in lines if "UPI-TEA" in line)
    credit: str = next(line for line in lines if "SALARY" in line)
    assert debit.index("100.00") != credit.index("500.00")
    assert abs(debit.index("100.00") - next(line for line in lines if "Withdrawal" in line).index("Withdrawal")) <= 2


# --- found in the whole-branch review --------------------------------------------------------------------

def test_amounts_with_an_attached_cr_or_dr_count_as_printed(llm_settings: Settings) -> None:
    card: list[Page] = read_pdf(make_pdf([{"texts": [
        (40, 30, "Card statement"), (40, 45, "Previous dues 1,000.00Dr"), (40, 60, "Total due 700.00Dr"),
        (40, 90, "01/09/26"), (120, 90, "PAYMENT THANK YOU"), (400, 90, "300.00Cr")]}]))
    answer: dict[str, Any] = changed(account_kind="credit_card", opening=100000, closing=70000, rows=[
        {**ROW, "narration": "PAYMENT THANK YOU", "direction": "credit", "amount": 30000, "balance": None,
         "bill_payment": True}])
    got: ModelRead = read(llm_settings, answer, pages=card)
    assert [r.amount for r in got.result.rows] == [30000] and got.result.validation["outcome"] == "passed"


def test_a_row_whose_date_is_not_printed_on_its_page_cannot_be_read(llm_settings: Settings) -> None:
    misdated: dict[str, Any] = {**ROW, "date": "2026-01-09"}  # 01/09 read month-first
    got: ModelRead = read(llm_settings, changed(rows=[misdated, ANSWER["rows"][1]]))
    assert [r.date for r in got.result.rows] == ["2026-09-02"]
    assert "date 2026-01-09 isn't printed on page 1" in got.result.unparseable[0]["reason"]


@pytest.mark.parametrize("printed", ["05 Sep 26", "5-Sep-2026", "05/09/2026", "05.09.2026", "05SEP26", "Sep 5, 2026"])
def test_common_indian_date_forms_count_as_printed(llm_settings: Settings, printed: str) -> None:
    page: list[Page] = read_pdf(make_pdf([{"texts": [(40, 30, "Bank"), (40, 90, printed), (200, 90, "TEA"),
                                                     (400, 90, "100.00")]}]))
    got: ModelRead = read(llm_settings, changed(opening=None, closing=None, rows=[{**ROW, "date": "2026-09-05"}]),
                          pages=page)
    assert len(got.result.rows) == 1, got.result.unparseable


def _word(text: str, x0: float, top: float, per_char: float = 3.3) -> Word:
    return Word(text, x0, x0 + per_char * len(text), top, top + 7)


def _line(top: float, *words: Word) -> Line:
    return Line(" ".join(w.text for w in words), top, top + 7, words)


def test_layout_text_keeps_columns_apart_in_small_narrow_type() -> None:
    """Amounts are right-aligned under their headers; with 3.3pt characters (7pt condensed type) a fixed 4.5pt
    column made a long narration push the debit amount toward the credit column."""
    header: Line = _line(10, _word("Date", 30, 10), _word("Narration", 80, 10), _word("Withdrawal", 267, 10),
                         _word("Deposit", 337, 10), _word("Balance", 417, 10))
    long_words: list[Word] = [_word(w, x, 25) for w, x in (("upi/p2m/612345678901/swiggy", 80), ("instamart/payment", 172),
                                                          ("from", 231), ("phone", 247))]
    debit: Line = _line(25, _word("01/09/26", 30, 25), *long_words, _word("100.00", 300 - 19.8, 25),
                        _word("9,900.00", 440 - 26.4, 25))
    credit: Line = _line(40, _word("02/09/26", 30, 40), _word("salary", 80, 40), _word("500.00", 360 - 19.8, 40),
                         _word("10,400.00", 440 - 29.7, 40))
    text: list[str] = layout_text(Page(1, (header, debit, credit), ())).splitlines()
    assert text[1].index("100.00") + len("100.00") <= text[0].index("Deposit")  # stays in the Withdrawal column
    assert text[2].index("500.00") >= text[0].index("Withdrawal") + len("Withdrawal")
    assert text[1].index("9,900.00") + 8 == text[2].index("10,400.00") + 9  # right edges line up
