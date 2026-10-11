"""The statement interpreter (tech-design §4.7, amended 2026-10-11).

    detect          every all_of phrase, no none_of phrase, on the spec's page
    bind account    account_binding pattern over the whole text, once (a statement is one account)
    per table       from each header row to the first ends_at line (or page end);
                    on a page with no header row, after the resumes_after line
      rows          ruled: words cut into cells at the printed column rules
                    line_pattern: one RE2 match per line, named groups are fields
      skip_matching dropped and counted; stop_at ends the whole statement
      continuation  empty_date_column: a line with no date continues the row above,
                    across a page break too; none: such a line is unparseable
    finish rows     amount style -> direction + amount text, then field transforms
    validate        declared balances vs the rows (validation.py, §4.8)

Structural surprises (no header row, a column count or header label that does
not match) raise TemplateMismatch: the template does not fit this statement and
the caller falls through to tier 3. A single bad row never does that; it is
kept, with its raw cells, in `unparseable` for the diagnostic.
"""

import re
from dataclasses import dataclass, field

from hisaab.parsing import patterns
from hisaab.parsing.pdf import Line, Page
from hisaab.parsing.statement_spec import StatementSpec
from hisaab.parsing.transforms import TransformError
from hisaab.parsing.validation import validate

RULE_MERGE = 2.0  # points: rules closer than this are one column edge


class TemplateMismatch(Exception):
    """This template does not fit this statement (message says why)."""


class RowError(ValueError):
    """One row can't be read (message goes into the diagnostic)."""


@dataclass(frozen=True)
class StatementRow:
    page: int
    date: str
    time: str | None
    narration: str
    reference: str | None
    direction: str  # debit | credit
    amount: int  # paise, positive
    balance: int | None


@dataclass
class StatementResult:
    account: tuple[str, str] | None  # (kind, identifier) for the registry, or None: ask the user
    rows: list[StatementRow]
    unparseable: list[dict]  # {"page", "raw_cells", "reason"}
    rows_skipped: int
    pages_processed: int
    validation: dict = field(default_factory=dict)  # the §4.8 diagnostic


@dataclass(frozen=True)
class StatementTemplate:
    id: str
    name: str
    spec: StatementSpec


def pick_statement_template(candidates: list[StatementTemplate], pages: list[Page]) -> StatementTemplate | None:
    """§4.7 step 2: the first candidate (already in priority order) whose detect matches; None -> tier 3."""
    return next((t for t in candidates if detect(t.spec, pages)), None)


def detect(spec: StatementSpec, pages: list[Page]) -> bool:
    if len(pages) < spec.detect_page:
        return False
    text = pages[spec.detect_page - 1].text
    return all(p in text for p in spec.all_of) and not any(p in text for p in spec.none_of)


def interpret_statement(spec: StatementSpec, pages: list[Page], *, template_id: str | None = None,
                        template_name: str | None = None, tolerance_paise: int = 100) -> StatementResult:
    full_text = "\n".join(p.text for p in pages)
    raw_rows, skipped, pages_seen = _raw_rows(spec, pages)
    rows, unparseable = [], []
    for raw in raw_rows:
        try:
            rows.append(_finish(spec, raw))
        except RowError as e:
            unparseable.append({"page": raw["page"], "raw_cells": raw["raw_cells"], "reason": str(e)})
    result = StatementResult(_bind(spec, full_text), rows, unparseable, skipped, pages_seen)
    result.validation = validate(spec, full_text, result, template_id=template_id, template_name=template_name,
                                 tolerance_paise=tolerance_paise)
    return result


# --- account binding --------------------------------------------------------------------

def _bind(spec: StatementSpec, text: str) -> tuple[str, str] | None:
    if spec.binding is None:
        return None
    try:
        m = patterns.search(spec.binding.pattern, text)
        return (spec.binding_kind, spec.binding.transform(m.group(1))) if m and m.group(1) else None
    except (patterns.PatternTimeout, TransformError):
        return None


# --- rows -------------------------------------------------------------------------------

def _matches(compiled, text: str) -> bool:
    try:
        return patterns.search(compiled, text) is not None
    except patterns.PatternTimeout:
        return False


def _is_header(spec: StatementSpec, line: Line) -> bool:
    return all(label in line.text for label in spec.table.header_contains)


def _raw_rows(spec: StatementSpec, pages: list[Page]) -> tuple[list[dict], int, int]:
    """Rows as {"page", "fields": {name: text}, "raw_cells": [...]}, continuation already folded."""
    rows: list[dict] = []
    skipped = 0
    found_header = False
    pages_seen = 0
    edges = None  # ruled: column edges, kept for pages where the table resumes without a header
    for page in pages:
        pages_seen += 1
        in_table = False
        for line in page.lines:
            if _is_header(spec, line):
                found_header, in_table = True, True
                if spec.table.rows == "ruled":
                    edges = _column_edges(spec, page, line)
                continue
            if not in_table:
                resumes = spec.table.resumes_after
                if found_header and resumes is not None and _matches(resumes, line.text):
                    in_table = True
                continue
            if spec.table.ends_at is not None and _matches(spec.table.ends_at, line.text):
                in_table = False
                continue
            if spec.stop_at is not None and _matches(spec.stop_at, line.text):
                if not found_header:
                    break
                return rows, skipped, pages_seen
            if any(_matches(p, line.text) for p in spec.skip_matching):
                skipped += 1
                continue
            fields, cells = _cells(spec, line, edges)
            if fields is None or not fields.get("date"):
                _continue_or_flag(spec, rows, page.number, fields, cells)
                continue
            rows.append({"page": page.number, "fields": fields, "raw_cells": cells})
    if not found_header:
        raise TemplateMismatch(f"no header row containing {list(spec.table.header_contains)} on any page")
    return rows, skipped, pages_seen


def _column_edges(spec: StatementSpec, page: Page, header: Line) -> list[float]:
    """The x of every vertical rule that reaches the header row or below, merged when close."""
    xs = sorted(r.x for r in page.rules if r.bottom > header.top)
    edges: list[float] = []
    for x in xs:
        if not edges or x - edges[-1] > RULE_MERGE:
            edges.append(x)
    count = len(edges) - 1
    if count != len(spec.table.columns):
        raise TemplateMismatch(f"page {page.number}: the table has {max(count, 0)} columns, the spec lists "
                               f"{len(spec.table.columns)}")
    cells = _split(header, edges)
    for i, column in enumerate(spec.table.columns):
        if column.label not in cells[i]:
            raise TemplateMismatch(f"page {page.number}: header label '{column.label}' is not in column {i + 1} "
                                   f"(found '{cells[i]}')")
    return edges


def _split(line: Line, edges: list[float]) -> list[str]:
    cells: list[list[str]] = [[] for _ in range(len(edges) - 1)]
    for word in line.words:
        for i in range(len(edges) - 1):
            if edges[i] <= word.x0 + 0.5 < edges[i + 1]:
                cells[i].append(word.text)
                break
    return [" ".join(c) for c in cells]


def _cells(spec: StatementSpec, line: Line, edges) -> tuple[dict | None, list[str]]:
    if spec.table.rows == "ruled":
        cells = _split(line, edges)
        fields = {c.field: cells[i] for i, c in enumerate(spec.table.columns) if c.field}
        return fields, cells
    try:
        m = patterns.search(spec.table.line_pattern, line.text)
    except patterns.PatternTimeout:
        m = None
    if m is None:
        return None, [line.text]
    return {k: (v or "") for k, v in m.groupdict().items()}, [line.text]


def _continue_or_flag(spec: StatementSpec, rows: list[dict], page: int, fields: dict | None, cells: list[str]):
    """A line without a date: the rest of the previous row's narration, or unreadable."""
    if spec.continuation == "empty_date_column" and rows:
        if fields is None:  # line_pattern: the whole line is narration
            extra = cells[0]
        else:
            others = [v for k, v in fields.items() if k != "narration" and v]
            extra = fields.get("narration", "") if not others else None
        if extra:
            # Wrapped at a fixed width, often mid-token ("...SHOP@OK" + "AXIS-UPI"), so no separator.
            rows[-1]["fields"]["narration"] += extra
            rows[-1]["raw_cells"] = rows[-1]["raw_cells"] + cells
            return
    rows.append({"page": page, "fields": None, "raw_cells": cells})


# --- finishing a row --------------------------------------------------------------------

_SIGN = re.compile(r"^([+-])\s*")
_SUFFIX = re.compile(r"^(.*?)\s*\b(Cr|CR|cr|C|Dr|DR|dr|D)\.?$")


def amount_with_direction(style: str, fields: dict, unsigned_means: str | None) -> tuple[str, str]:
    """The three amount styles (§4.6): which way the money went, and the amount text left to convert."""
    if style == "separate_debit_credit":
        debit, credit = fields.get("debit", "").strip(), fields.get("credit", "").strip()
        if debit and credit:
            raise RowError("both debit and credit are filled")
        if not (debit or credit):
            raise RowError("neither debit nor credit is filled")
        return ("debit", debit) if debit else ("credit", credit)
    text = fields.get("amount", "").strip()
    if not text:
        raise RowError("no amount")
    if style == "signed_single_column":
        m = _SIGN.match(text)
        if m:
            return ("credit" if m.group(1) == "+" else "debit"), text[m.end():]
        return unsigned_means, text
    m = _SUFFIX.match(text)  # cr_dr_suffix: "2,050.00 Cr", or SBI's "2,050.00 C"
    if m:
        return ("credit" if m.group(2).lower().startswith("c") else "debit"), m.group(1)
    return unsigned_means, text


def _finish(spec: StatementSpec, raw: dict) -> StatementRow:
    fields = raw["fields"]
    if fields is None:
        raise RowError("line does not fit the table" if spec.table.rows == "line_pattern" else "row has no date")
    direction, amount_text = amount_with_direction(spec.amount_style, fields, spec.unsigned_means)
    amount_key = "amount" if spec.amount_style != "separate_debit_credit" else direction

    def convert(name: str, text: str):
        try:
            return spec.transforms[name](text)
        except TransformError as e:
            raise RowError(f"{name}: {e}") from None

    amount = convert(amount_key, amount_text)
    if amount <= 0:
        raise RowError("amount is zero")
    time_text, balance_text = fields.get("time", ""), fields.get("balance", "")
    return StatementRow(
        page=raw["page"],
        date=convert("date", fields["date"]),
        time=convert("time", time_text) if time_text else None,
        narration=fields.get("narration", "").strip(),
        reference=fields.get("reference", "").strip() or None,
        direction=direction,
        amount=amount,
        balance=convert("balance", balance_text) if balance_text else None,
    )
