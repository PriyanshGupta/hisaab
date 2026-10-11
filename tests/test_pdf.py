"""Reading statement PDFs: passwords, text lines from word positions, printed column rules."""

import pytest

from hisaab.parsing.pdf import NotAPdf, PasswordRequired, WrongPassword, read_pdf
from tests.pdf_factory import make_pdf

PAGE = {
    "texts": [(40, 100, "Date"), (120, 100, "Narration"), (40, 115, "01/07/26"), (120, 116, "Tea stall"),
              (300, 115, "40.00")],
    "vlines": [(30, 95, 130), (110, 95, 130), (290, 95, 130)],
}


def test_lines_are_built_from_words_on_the_same_baseline():
    (page,) = read_pdf(make_pdf([PAGE]))
    assert [line.text for line in page.lines] == ["Date Narration", "01/07/26 Tea stall 40.00"]  # 1pt apart is one line
    assert page.number == 1
    assert page.text == "Date Narration\n01/07/26 Tea stall 40.00"
    first = page.lines[1].words[0]
    assert (first.text, round(first.x0)) == ("01/07/26", 40)


def test_vertical_rules_are_reported_with_their_extent():
    (page,) = read_pdf(make_pdf([PAGE]))
    assert [(round(r.x), round(r.top), round(r.bottom)) for r in page.rules] == [(30, 95, 130), (110, 95, 130), (290, 95, 130)]


def test_a_rupee_symbol_font_that_draws_the_sign_as_C_reads_as_the_rupee_sign():
    # The HDFC card statement prints ₹ with a symbol font whose character code is "C".
    (page,) = read_pdf(make_pdf([{"texts": [(200, 100, "1,234.00")], "rupee_texts": [(190, 100, "C")]}]))
    assert page.lines[0].text == "₹ 1,234.00"


def test_pages_come_back_in_order():
    pages = read_pdf(make_pdf([{"texts": [(40, 100, "one")]}, {"texts": [(40, 100, "two")]}]))
    assert [(p.number, p.text) for p in pages] == [(1, "one"), (2, "two")]


def test_a_protected_pdf_opens_with_its_password():
    data = make_pdf([PAGE], password="s3cret")
    assert read_pdf(data, "s3cret")[0].lines[0].text == "Date Narration"


def test_a_protected_pdf_without_a_password_asks_for_one():
    with pytest.raises(PasswordRequired):
        read_pdf(make_pdf([PAGE], password="s3cret"))


def test_a_wrong_password_is_told_apart_from_a_missing_one():
    with pytest.raises(WrongPassword):
        read_pdf(make_pdf([PAGE], password="s3cret"), "guess")


def test_something_that_is_not_a_pdf():
    with pytest.raises(NotAPdf):
        read_pdf(b"%PDF-1.4 this is not really a pdf")
    with pytest.raises(NotAPdf):
        read_pdf(b"hello")
