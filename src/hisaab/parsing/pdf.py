"""Reading statement PDFs (tech-design §4.7 step 1).

A statement is opened from bytes with the user's password (never stored,
PRD §4.6) and reduced to what the statement interpreter needs from each page:

- lines: pdfplumber's words grouped by baseline, left to right. The text of a
  line is its words joined by single spaces.
- rules: the vertical lines printed on the page (thin rects count: the HDFC
  savings statement draws its column dividers as 1pt-wide rectangles). In a
  ruled table these are the column edges.

pdfplumber's own table finder is not used: on the fixture statements it either
dropped a column or split words (ruling 2026-10-11). Words and rules are the
reliable layer, and the spec decides how to cut them into rows.
"""

import io
from dataclasses import dataclass

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfplumber.page import Page as PdfPage

LINE_TOLERANCE: float = 3  # points: words whose tops differ by less sit on one line


class PdfError(ValueError):
    """The file can't be read as a statement (message is user-facing)."""


class PasswordRequired(PdfError):
    pass


class WrongPassword(PdfError):
    pass


class NotAPdf(PdfError):
    pass


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float


@dataclass(frozen=True)
class Line:
    text: str
    top: float
    bottom: float
    words: tuple[Word, ...]


@dataclass(frozen=True)
class Rule:
    x: float
    top: float
    bottom: float


@dataclass(frozen=True)
class Page:
    number: int  # 1-based
    lines: tuple[Line, ...]
    rules: tuple[Rule, ...]

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


def read_pdf(data: bytes, password: str | None = None) -> list[Page]:
    try:
        with pdfplumber.open(io.BytesIO(data), password=password or "") as pdf:
            return [_page(i + 1, p) for i, p in enumerate(pdf.pages)]
    except Exception as e:  # pdfplumber wraps every pdfminer failure in PdfminerException
        cause: object = e.args[0] if e.args else None
        if isinstance(cause, PDFPasswordIncorrect) or isinstance(e, PDFPasswordIncorrect):
            if password:
                raise WrongPassword("That password doesn't open this statement.") from None
            raise PasswordRequired("This statement is password-protected. Enter its password.") from None
        raise NotAPdf("This file isn't a PDF that can be read.") from None


def _page(number: int, page: PdfPage) -> Page:
    words: list[Word] = []
    for w in page.extract_words(extra_attrs=["fontname"]):  # a font change always starts a new word
        # A symbol font draws ₹ with the character code of "C" (HDFC card statement).
        text: str = "₹" if "rupee" in (w.get("fontname") or "").lower() else w["text"]
        words.append(Word(text, w["x0"], w["x1"], w["top"], w["bottom"]))
    words.sort(key=lambda w: (w.top, w.x0))
    lines: list[list[Word]] = []
    for word in words:
        if lines and abs(word.top - lines[-1][0].top) < LINE_TOLERANCE:
            lines[-1].append(word)
        else:
            lines.append([word])
    built: list[Line] = []
    for group in lines:
        group.sort(key=lambda w: w.x0)
        built.append(Line(" ".join(w.text for w in group), group[0].top, max(w.bottom for w in group),
                          tuple(group)))
    rules: list[Rule] = [Rule(ln["x0"], ln["top"], ln["bottom"]) for ln in page.lines if abs(ln["x1"] - ln["x0"]) < 1]
    rules += [Rule((r["x0"] + r["x1"]) / 2, r["top"], r["bottom"]) for r in page.rects if r["width"] <= 2]
    rules.sort(key=lambda r: (r.x, r.top))
    return Page(number, tuple(built), tuple(rules))
