"""The transform registry (tech-design §4.4).

The rule: a template holds selection, structure and wiring; code holds named,
individually tested capabilities. A template names a transform; it never
contains logic. Anything bespoke to one bank becomes an entry here.

Two vocabularies:
- field transforms: one captured string in, one contract value out (int paise,
  ISO date, canonical VPA...). Raising TransformError means the field failed
  (§4.3 step 4). Messages never repeat the captured text, because failure
  reasons end up in diagnostics that outlive the email.
- preprocessors: the whole body in, text out, applied in the order the spec
  lists them (§4.2). Bank alerts are HTML; patterns are written against the
  text that strip_html + collapse_whitespace leave.

`date:<fmt>` and `time:<fmt>` are parameterised: the parameter is a strptime
format the template pins, so no date format is ever guessed (§4.2, §4.8).

The vocabulary covers the fixture emails and nothing more (§4.4). Amount forms
seen across the fixtures: "Rs.179.00", "Rs. 1234.00", "INR 9,99,999.99",
"₹9,999.99", "₹9,999" (no paise), with or without Indian digit grouping.
Statements add Western grouping: the HDFC savings statement prints "212,699.42".
"""

import re
from collections.abc import Callable
from datetime import datetime
from functools import partial
from html.parser import HTMLParser

from hisaab.accounts import identifiers


class TransformError(ValueError):
    """The captured text is not a valid value for this transform; the field failed."""


class UnknownTransform(ValueError):
    """A name that is not in the registry (rejected when a template is saved)."""


# --- field transforms ---------------------------------------------------------------

_CURRENCY_PREFIX = re.compile(r"^(?:Rs\.?|INR|₹)\s*", re.IGNORECASE)  # \s covers U+00A0 and U+202F
# Plain digits; Indian grouping, 1-2 digits then pairs then a final three ("1,23,456");
# or Western grouping, threes ("123,456"). Ill-formed grouping is a mis-capture and fails.
_RUPEES = re.compile(r"(\d+|\d{1,2}(?:,\d{2})*,\d{3}|\d{1,3}(?:,\d{3})+)(?:\.(\d{1,2}))?")


def inr_to_paise(raw: str) -> int:
    """'₹1,23,456.78' -> 12345678. String arithmetic only: no float ever touches money."""
    m = _RUPEES.fullmatch(_CURRENCY_PREFIX.sub("", raw.strip()))
    if not m:
        raise TransformError("not a rupee amount")
    rupees, paise = m.group(1).replace(",", ""), (m.group(2) or "").ljust(2, "0")
    return int(rupees) * 100 + int(paise)


def digits_only(raw: str) -> str:
    """'XX1234' -> '1234': drops the masking banks print around account and card digits."""
    digits = re.sub(r"\D", "", raw)
    if not digits:
        raise TransformError("no digits")
    return digits


def normalize_vpa(raw: str) -> str:
    """The same canonical form the accounts registry stores, so exact matching works."""
    try:
        return identifiers.normalize("vpa", raw)
    except identifiers.IdentifierError:
        raise TransformError("not a UPI ID") from None


def _date(fmt: str, raw: str) -> str:
    try:
        return datetime.strptime(raw.strip(), fmt).date().isoformat()  # strptime refuses 31-02, no rollover
    except ValueError:
        raise TransformError(f"not a date in the form {fmt}") from None


def _time(fmt: str, raw: str) -> str:
    try:
        return datetime.strptime(raw.strip(), fmt).time().isoformat()
    except ValueError:
        raise TransformError(f"not a time in the form {fmt}") from None


FIELD_TRANSFORMS: dict[str, Callable[[str], object]] = {
    "inr_to_paise": inr_to_paise,
    "digits_only": digits_only,
    "normalize_vpa": normalize_vpa,
    "upper": str.upper,
    "trim": str.strip,
}
_PARAMETERISED = {"date": _date, "time": _time}


def field_transform(name: str) -> Callable[[str], object]:
    if name in FIELD_TRANSFORMS:
        return FIELD_TRANSFORMS[name]
    kind, sep, fmt = name.partition(":")
    if sep and kind in _PARAMETERISED and "%" in fmt:
        return partial(_PARAMETERISED[kind], fmt)
    raise UnknownTransform(f"unknown transform '{name}'")


# --- preprocessors ------------------------------------------------------------------

_BLOCK_TAGS = {"br", "p", "div", "tr", "td", "th", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6"}
_HIDDEN_TAGS = {"head", "style", "script"}


class _TextExtractor(HTMLParser):
    """Visible text only. Block tags become spaces so table cells don't run together;
    inline tags (b, span, a) add nothing, so "Rs.<b>179</b>" stays "Rs.179"."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)  # &nbsp; -> U+00A0, &amp; -> &
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in _HIDDEN_TAGS:
            self.hidden += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in _HIDDEN_TAGS:
            self.hidden = max(0, self.hidden - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def strip_html(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return "".join(parser.parts)


def collapse_whitespace(text: str) -> str:
    return " ".join(text.split())  # str.split() treats U+00A0 and U+202F as whitespace


PREPROCESSORS: dict[str, Callable[[str], str]] = {
    "strip_html": strip_html,
    "collapse_whitespace": collapse_whitespace,
}


def preprocessor(name: str) -> Callable[[str], str]:
    if name not in PREPROCESSORS:
        raise UnknownTransform(f"unknown preprocessor '{name}'")
    return PREPROCESSORS[name]
