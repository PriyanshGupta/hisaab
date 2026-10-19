"""Statement template specs (tech-design §4.6, amended by ruling 2026-10-11) and
their save-time validation.

    {"version": 1, "artifact_type": "statement",
     "detect": {"page": 1, "all_of": [...], "none_of": [...]},      substrings of that page's text
     "account_binding": {"identifier": {"pattern", "transform", "kind": "last4" | "card_last4"}},
     "account_kind": "bank" | "credit_card",                          decides the arithmetic sign
     "table": {"rows": "ruled" | "line_pattern", "ends_at": "<RE2>", ...},
     "row_rules": {"amount_style", "unsigned_means", "continuation", "skip_matching", "stop_at",
                   "payment_matching"},                               card statements only, optional
     "field_transforms": {"date": "date:%d/%m/%y", "debit": "inr_to_paise", ...},
     "declared_balances": {"opening": {"pattern", "transform"}, "closing": {...}}}   optional

The amendment: §4.6 put pdfplumber's table-finder settings here. On the real
statements that finder dropped a column (HDFC savings) or split words and had
no fixed geometry (HDFC card: page 1's table sits elsewhere). Rows are built
from words instead, in one of two modes:

- "ruled": the table's column edges are the vertical rules printed on the page.
  "columns" lists them left to right as {"label": <header text>, "field": <row
  field or omitted>}. The header row is the line containing every label, and
  each label must sit in its own column: the sanity check that catches a bank
  reordering columns (§4.7).
- "line_pattern": each table line is matched against an RE2 pattern whose named
  groups are row fields. "header_row.contains" finds the header line.

A table runs from each header row to the first line matching "ends_at" (page
footers), or the page end. Some banks print the header row on the first page
only (HDFC savings): "resumes_after" names the line after which the open table
carries on, on a page with no header row, with the same columns. Row fields: date, time, narration, reference, debit,
credit, amount, balance.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hisaab.parsing.patterns import Pattern, PatternError, compile_pattern
from hisaab.parsing.transforms import UnknownTransform, field_transform

ROW_FIELDS: tuple[str, ...] = ("date", "time", "narration", "reference", "debit", "credit", "amount", "balance")
AMOUNT_STYLES: tuple[str, ...] = ("separate_debit_credit", "signed_single_column", "cr_dr_suffix")
CONTINUATIONS: tuple[str, ...] = ("empty_date_column", "none")
ACCOUNT_KINDS: tuple[str, ...] = ("bank", "credit_card")
BINDING_KINDS: tuple[str, ...] = ("last4", "card_last4")
_TYPED: dict[str, str] = {"date": "date:", "time": "time:", "debit": "inr_to_paise", "credit": "inr_to_paise",
                          "amount": "inr_to_paise", "balance": "inr_to_paise"}


@dataclass(frozen=True)
class Column:
    label: str
    field: str | None


@dataclass(frozen=True)
class Table:
    rows: str
    columns: tuple[Column, ...]  # ruled
    header_contains: tuple[str, ...]  # the labels that identify a header line
    line_pattern: Pattern | None  # line_pattern
    ends_at: Pattern | None
    resumes_after: Pattern | None


@dataclass(frozen=True)
class Extractor:
    pattern: Pattern
    transform: Callable[[str], object]


@dataclass(frozen=True)
class StatementSpec:
    detect_page: int
    all_of: tuple[str, ...]
    none_of: tuple[str, ...]
    binding: Extractor | None
    binding_kind: str | None
    account_kind: str
    table: Table
    amount_style: str
    unsigned_means: str | None
    continuation: str
    skip_matching: tuple[Pattern, ...]
    stop_at: Pattern | None
    transforms: dict[str, Callable[[str], object]]  # row field -> transform
    declared: dict[str, Extractor]  # "opening" / "closing"
    # Card statements: a credit whose narration matches one of these is a bill payment received, not a
    # refund (ruling 2026-10-19). The phrases are the bank's own wording, taken from the fixtures.
    payment_matching: tuple[Pattern, ...] = ()


class InvalidStatementSpec(ValueError):
    def __init__(self, problems: list[str]) -> None:
        self.problems: list[str] = problems
        super().__init__("; ".join(problems))


def parse_statement_spec(spec: object) -> StatementSpec:
    parsed, problems = check_statement_spec(spec)
    if parsed is None:
        raise InvalidStatementSpec(problems)
    return parsed


def _phrases(value: object, *, non_empty: bool) -> list[str] | None:
    """value as a list of non-empty strings, or None when it isn't one."""
    if isinstance(value, list) and (value or not non_empty) and all(isinstance(s, str) and s for s in value):
        return [str(s) for s in value]
    return None


def check_statement_spec(spec: object) -> tuple[StatementSpec | None, list[str]]:
    """The parsed spec, or None with every problem found."""
    if not isinstance(spec, dict):
        return None, ["spec must be a JSON object"]
    problems: list[str] = []

    def pattern(where: str, text: object, groups: int = 0) -> Pattern | None:
        try:
            return compile_pattern(text, groups=groups)
        except PatternError as e:
            problems.append(f"{where}: {e}")
            return None

    def obj(where: str) -> dict[str, Any]:
        value: object = spec.get(where, {})
        if not isinstance(value, dict):
            problems.append(f"{where}: must be an object")
            return {}
        return value

    if spec.get("version") != 1:
        problems.append("version: must be 1")
    if spec.get("artifact_type") != "statement":
        problems.append('artifact_type: must be "statement"')

    detect: dict[str, Any] = obj("detect")
    page: object = detect.get("page", 1)
    if not (isinstance(page, int) and page >= 1):
        problems.append("detect.page: must be a page number from 1")
    all_of: list[str] | None = _phrases(detect.get("all_of"), non_empty=True)
    if all_of is None:
        problems.append("detect.all_of: must list at least one phrase")
    none_of: list[str] | None = _phrases(detect.get("none_of", []), non_empty=False)
    if none_of is None:
        problems.append("detect.none_of: must be a list of phrases")

    binding: Extractor | None = None
    binding_kind: object = None
    if "account_binding" in spec:
        ident: object = obj("account_binding").get("identifier")
        if not isinstance(ident, dict):
            problems.append("account_binding: needs an identifier {pattern, transform, kind}")
        else:
            binding_kind = ident.get("kind")
            if binding_kind not in BINDING_KINDS:
                problems.append(f"account_binding: kind must be one of {', '.join(BINDING_KINDS)}")
            binding = _extractor("account_binding", ident, problems, pattern, required_transform=None)

    account_kind: object = spec.get("account_kind")
    if account_kind not in ACCOUNT_KINDS:
        problems.append(f"account_kind: must be one of {', '.join(ACCOUNT_KINDS)}")

    table_in: dict[str, Any] = obj("table")
    rows: object = table_in.get("rows")
    columns: list[Column] = []
    header_contains: list[str] = []
    line_pattern: Pattern | None = None
    fields: list[str] = []
    if rows == "ruled":
        cols: object = table_in.get("columns")
        if not (isinstance(cols, list) and cols):
            problems.append("table.columns: must list the columns left to right")
            cols = []
        for i, col in enumerate(cols):
            label: object = col.get("label") if isinstance(col, dict) else None
            if not (isinstance(label, str) and label.strip()):
                problems.append(f"table.columns[{i}]: needs a label (its header text)")
                continue
            field: object = col.get("field")
            if field is not None and field not in ROW_FIELDS:
                problems.append(f"table.columns[{i}]: unknown row field '{field}'")
            columns.append(Column(label, field if isinstance(field, str) else None))
            header_contains.append(label)
        fields = [c.field for c in columns if c.field]
    elif rows == "line_pattern":
        header_row: object = table_in.get("header_row")
        contains: list[str] | None = (
            _phrases(header_row.get("contains"), non_empty=True) if isinstance(header_row, dict) else None
        )
        if contains is None:
            problems.append("table.header_row: needs contains: [the header line's labels]")
        else:
            header_contains = contains
        line_pattern = pattern("table.line_pattern", table_in.get("line_pattern"))
        if line_pattern is not None:
            fields = list(line_pattern.groupindex)
            for name in fields:
                if name not in ROW_FIELDS:
                    problems.append(f"table.line_pattern: unknown row field '{name}'")
    else:
        problems.append("table.rows: must be ruled or line_pattern")
    for name in {f for f in fields if fields.count(f) > 1}:
        problems.append(f"table: field '{name}' is mapped more than once")
    ends_at: Pattern | None = pattern("table.ends_at", table_in["ends_at"]) if "ends_at" in table_in else None
    resumes: Pattern | None = (
        pattern("table.resumes_after", table_in["resumes_after"]) if "resumes_after" in table_in else None
    )

    rules: dict[str, Any] = obj("row_rules")
    style: object = rules.get("amount_style")
    if style not in AMOUNT_STYLES:
        problems.append(f"row_rules.amount_style: must be one of {', '.join(AMOUNT_STYLES)}")
    unsigned: object = rules.get("unsigned_means")
    if style in ("signed_single_column", "cr_dr_suffix") and unsigned not in ("debit", "credit"):
        problems.append("row_rules.unsigned_means: debit or credit, for amounts with no sign or suffix")
    continuation: object = rules.get("continuation")
    if continuation not in CONTINUATIONS:
        problems.append(f"row_rules.continuation: must be one of {', '.join(CONTINUATIONS)}")
    skip: object = rules.get("skip_matching", [])
    skip_compiled: list[Pattern | None] = (
        [pattern("row_rules.skip_matching", s) for s in skip] if isinstance(skip, list) else []
    )
    stop_at: Pattern | None = pattern("row_rules.stop_at", rules["stop_at"]) if "stop_at" in rules else None
    paying: object = rules.get("payment_matching", [])
    paying_compiled: list[Pattern | None] = (
        [pattern("row_rules.payment_matching", s) for s in paying] if isinstance(paying, list) else []
    )
    if paying and account_kind != "credit_card":
        problems.append("row_rules.payment_matching: only card statements receive bill payments")

    needed: list[str] = ["date", "narration"] + (
        ["debit", "credit"] if style == "separate_debit_credit" else ["amount"])
    if rows in ("ruled", "line_pattern"):
        for name in needed:
            if name not in fields:
                problems.append(f"table: rows need a '{name}' field for {style or 'this amount style'}")

    transforms: dict[str, Callable[[str], object]] = {}
    tf: dict[str, Any] = obj("field_transforms")
    for name, tname in tf.items():
        if name not in ROW_FIELDS:
            problems.append(f"field_transforms.{name}: not a row field")
            continue
        family: str | None = _TYPED.get(name)
        if family and not (isinstance(tname, str) and tname.startswith(family)):
            suffix: str = "<format>" if family.endswith(":") else ""
            problems.append(f"field_transforms.{name}: transform must be {family}{suffix}")
            continue
        try:
            if not isinstance(tname, str):
                raise UnknownTransform(str(tname))
            transforms[name] = field_transform(tname)
        except UnknownTransform:
            problems.append(f"field_transforms.{name}: unknown transform '{tname}'")
    for name in fields:
        if name in _TYPED and name not in tf:
            problems.append(f"field_transforms.{name}: required, the row's {name} must convert to its type")

    declared: dict[str, Extractor] = {}
    for name, raw in obj("declared_balances").items():
        if name not in ("opening", "closing"):
            problems.append(f"declared_balances.{name}: only opening and closing")
            continue
        ex: Extractor | None = _extractor(f"declared_balances.{name}", raw, problems, pattern,
                                          required_transform="inr_to_paise")
        if ex:
            declared[name] = ex

    if problems:
        return None, problems
    # Every value below passed its check above (problems would be non-empty otherwise).
    assert isinstance(page, int) and all_of is not None and none_of is not None
    assert isinstance(rows, str) and isinstance(account_kind, str) and isinstance(style, str)
    assert isinstance(continuation, str) and (unsigned is None or isinstance(unsigned, str))
    assert binding_kind is None or isinstance(binding_kind, str)
    return StatementSpec(
        detect_page=page, all_of=tuple(all_of), none_of=tuple(none_of), binding=binding, binding_kind=binding_kind,
        account_kind=account_kind,
        table=Table(rows, tuple(columns), tuple(header_contains), line_pattern, ends_at, resumes),
        amount_style=style, unsigned_means=unsigned, continuation=continuation,
        skip_matching=tuple(p for p in skip_compiled if p is not None), stop_at=stop_at, transforms=transforms,
        declared=declared, payment_matching=tuple(p for p in paying_compiled if p is not None),
    ), problems


def _extractor(where: str, raw: object, problems: list[str], pattern: Callable[..., Pattern | None],
               required_transform: str | None) -> Extractor | None:
    if not isinstance(raw, dict):
        problems.append(f"{where}: must be {{pattern, transform}}")
        return None
    compiled: Pattern | None = pattern(where, raw.get("pattern"), groups=1)
    tname: object = raw.get("transform")
    if required_transform and tname != required_transform:
        problems.append(f"{where}: transform must be {required_transform}")
        return None
    try:
        if not isinstance(tname, str):
            raise UnknownTransform(str(tname))
        transform: Callable[[str], object] = field_transform(tname)
    except UnknownTransform:
        problems.append(f"{where}: unknown transform '{tname}'")
        return None
    return Extractor(compiled, transform) if compiled is not None else None
