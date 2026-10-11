"""The nine v0 transforms (tech-design §4.4), each tested on its own."""

import pytest

from hisaab.parsing.transforms import (
    FIELD_TRANSFORMS,
    PREPROCESSORS,
    TransformError,
    UnknownTransform,
    field_transform,
    preprocessor,
)


def test_the_registry_holds_exactly_the_nine_v0_entries():
    names = set(FIELD_TRANSFORMS) | set(PREPROCESSORS) | {"date", "time"}
    assert names == {"inr_to_paise", "digits_only", "normalize_vpa", "date", "time",
                     "strip_html", "collapse_whitespace", "upper", "trim"}


# --- inr_to_paise ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "paise"),
    [
        ("1,23,456.78", 12345678),  # Indian grouping: lakh then thousand
        ("₹2,050.00", 205000),
        ("Rs. 2,050.00", 205000),  # non-breaking space after the currency (HTML &nbsp;)
        (" ₹ 2,050.00 ", 205000),
        ("INR 649.00", 64900),  # narrow no-break space
        ("Rs. 179.00", 17900),
        ("Rs.48210.00", 4821000),  # no grouping at all
        ("INR 9,99,999.99", 99999999),
        ("₹9,999", 999900),  # no paise
        ("rs.5.5", 550),  # one decimal digit is tens of paise
        ("0.05", 5),
    ],
)
def test_inr_to_paise(raw, paise):
    assert field_transform("inr_to_paise")(raw) == paise


@pytest.mark.parametrize("raw", ["123,456", "1,2,3", "12,34", "", "Rs.", "-5.00", "12.345", "1 234", "five", "$5.00"])
def test_inr_to_paise_rejects_what_is_not_a_rupee_amount(raw):
    # 123,456 is Western grouping: no fixture uses it, so it signals a mis-capture rather than a number.
    with pytest.raises(TransformError):
        field_transform("inr_to_paise")(raw)


def test_inr_to_paise_never_uses_floats():
    # 0.29 * 100 == 28.999999999999996 in floating point
    assert field_transform("inr_to_paise")("0.29") == 29
    assert field_transform("inr_to_paise")("10,00,00,000.01") == 10_000_000_001


# --- identifiers ----------------------------------------------------------------------

@pytest.mark.parametrize(("raw", "out"), [("XX1234", "1234"), ("**** 1234", "1234"), ("1234", "1234")])
def test_digits_only_strips_masking(raw, out):
    assert field_transform("digits_only")(raw) == out


def test_digits_only_rejects_no_digits():
    with pytest.raises(TransformError):
        field_transform("digits_only")("XXXX")


def test_normalize_vpa_lowercases_and_trims():
    assert field_transform("normalize_vpa")("  Asha.K-1@OKHDFC ") == "asha.k-1@okhdfc"


def test_normalize_vpa_rejects_a_non_vpa():
    with pytest.raises(TransformError):
        field_transform("normalize_vpa")("not a vpa")


# --- dates and times ------------------------------------------------------------------

def test_date_parses_against_the_pinned_format():
    assert field_transform("date:%d-%m-%y")("10-10-26") == "2026-10-10"
    assert field_transform("date:%d/%m/%Y")(" 16/09/2026 ") == "2026-09-16"


@pytest.mark.parametrize("raw", ["31-02-26", "10-13-26", "2026-10-10", ""])
def test_date_rejects_out_of_range_or_other_formats_instead_of_rolling_over(raw):
    with pytest.raises(TransformError):
        field_transform("date:%d-%m-%y")(raw)


def test_time_parses_and_rejects_out_of_range():
    assert field_transform("time:%H:%M:%S")("18:44:58") == "18:44:58"
    assert field_transform("time:%H:%M")("09:05") == "09:05:00"
    with pytest.raises(TransformError):
        field_transform("time:%H:%M:%S")("25:00:00")


# --- trivial normalizers --------------------------------------------------------------

def test_upper_and_trim():
    assert field_transform("upper")("Swiggy") == "SWIGGY"
    assert field_transform("trim")("  Tea stall  ") == "Tea stall"


# --- names ----------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["lower", "date", "date:", "date:dd-mm-yy", "time:", "strip_html", "inr"])
def test_unknown_or_malformed_names_are_refused(name):
    with pytest.raises(UnknownTransform):
        field_transform(name)


def test_preprocessors_are_a_separate_vocabulary():
    with pytest.raises(UnknownTransform):
        preprocessor("trim")


# --- preprocessors --------------------------------------------------------------------

def test_strip_html_keeps_text_and_drops_markup_styles_and_head():
    html = (
        "<html><head><title>Alert</title><style>p{color:red}</style></head>"
        "<body><table><tr><td>Rs.&nbsp;<b>1,234.00</b></td><td>debited</td></tr></table>"
        "<p>Ref&amp;no</p><script>var x=1;</script></body></html>"
    )
    text = preprocessor("strip_html")(html)
    assert "Alert" not in text and "color" not in text and "var x" not in text and "<" not in text
    assert "Rs. 1,234.00" in text  # inline tags do not split words; entities unescaped
    assert "Ref&no" in text
    assert "1,234.00 debited" in " ".join(text.split())  # table cells are separated


def test_collapse_whitespace_folds_runs_including_nbsp():
    assert preprocessor("collapse_whitespace")("  Rs.  1,234 \n\n  debited\t") == "Rs. 1,234 debited"
