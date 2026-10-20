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
import re
from dataclasses import dataclass

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfplumber.page import Page as PdfPage

LINE_TOLERANCE: float = 3  # points: words whose tops differ by less sit on one line
CHAR_PT: float = 4.5  # points per character column in layout_text (about one character of 9pt type)
WORD_GAP_PT: float = 6.0  # layout_text: words closer than this are one phrase; farther, a new column


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


_NUMBER: re.Pattern[str] = re.compile(r"^[+-]?[\d,]*\d\.\d{2}(Cr|CR|Dr|DR|C|D)?$")


def layout_text(page: Page) -> str:
    """The page as text with each word at its horizontal position, so columns stay columns: for the model
    reading an unknown layout (tier 3), "40.00" under Withdrawal and under Deposit must not look the same.

    The character width is the page's narrowest (small or condensed type, common on statements), so text
    never overruns its column; amounts are placed by their right edge, as statements right-align them."""
    widths: list[float] = [(w.x1 - w.x0) / len(w.text) for line in page.lines for w in line.words if len(w.text) >= 4]
    char_pt: float = max(1.5, min([CHAR_PT, *widths]))
    out: list[str] = []
    for line in page.lines:
        text: str = ""
        previous: Word | None = None
        for word in line.words:
            right: bool = _NUMBER.match(word.text) is not None
            column: int = round(word.x1 / char_pt) - len(word.text) if right else round(word.x0 / char_pt)
            if previous is not None and word.x0 - previous.x1 < WORD_GAP_PT:
                text += " "  # the same phrase: one space, as printed
            elif previous is not None:  # a gap: the next column, at least two spaces away
                text += " " * max(2, column - len(text))
            else:
                text += " " * max(0, column)
            text += word.text
            previous = word
        out.append(text)
    return "\n".join(out)
