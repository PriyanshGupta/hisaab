"""Statement template specs (tech-design §4.6, amended by ruling 2026-10-11) and
their save-time validation.

    {"version": 1, "artifact_type": "statement",
     "detect": {"page": 1, "all_of": [...], "none_of": [...]},      substrings of that page's text
     "account_binding": {"identifier": {"pattern", "transform", "kind": "last4" | "card_last4"}},
     "account_kind": "bank" | "credit_card",                          decides the arithmetic sign
     "table": {"rows": "ruled" | "line_pattern", "ends_at": "<RE2>", ...},
     "row_rules": {"amount_style", "unsigned_means", "continuation", "skip_matching", "stop_at"},
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

from dataclasses import dataclass

from hisaab.parsing.patterns import PatternError, compile_pattern
from hisaab.parsing.transforms import UnknownTransform, field_transform

ROW_FIELDS = ("date", "time", "narration", "reference", "debit", "credit", "amount", "balance")
AMOUNT_STYLES = ("separate_debit_credit", "signed_single_column", "cr_dr_suffix")
CONTINUATIONS = ("empty_date_column", "none")
ACCOUNT_KINDS = ("bank", "credit_card")
BINDING_KINDS = ("last4", "card_last4")
_TYPED = {"date": "date:", "time": "time:", "debit": "inr_to_paise", "credit": "inr_to_paise",
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
    line_pattern: object | None  # line_pattern
    ends_at: object | None
    resumes_after: object | None


@dataclass(frozen=True)
class Extractor:
    pattern: object
    transform: object


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
    skip_matching: tuple[object, ...]
    stop_at: object | None
    transforms: dict  # row field -> callable
    declared: dict  # "opening" / "closing" -> Extractor


class InvalidStatementSpec(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def parse_statement_spec(spec: object) -> StatementSpec:
    parsed, problems = check_statement_spec(spec)
    if problems:
        raise InvalidStatementSpec(problems)
    return parsed


def check_statement_spec(spec: object) -> tuple[StatementSpec | None, list[str]]:
    if not isinstance(spec, dict):
        return None, ["spec must be a JSON object"]
    problems: list[str] = []

    def pattern(where: str, text, groups: int = 0):
        try:
            return compile_pattern(text, groups=groups)
        except PatternError as e:
            problems.append(f"{where}: {e}")
            return None

    def obj(where: str) -> dict:
        value = spec.get(where, {})
        if not isinstance(value, dict):
            problems.append(f"{where}: must be an object")
            return {}
        return value

    if spec.get("version") != 1:
        problems.append("version: must be 1")
    if spec.get("artifact_type") != "statement":
        problems.append('artifact_type: must be "statement"')

    detect = obj("detect")
    page = detect.get("page", 1)
    if not (isinstance(page, int) and page >= 1):
        problems.append("detect.page: must be a page number from 1")
    all_of, none_of = detect.get("all_of"), detect.get("none_of", [])
    if not (isinstance(all_of, list) and all_of and all(isinstance(s, str) and s for s in all_of)):
        problems.append("detect.all_of: must list at least one phrase")
        all_of = []
    if not (isinstance(none_of, list) and all(isinstance(s, str) and s for s in none_of)):
        problems.append("detect.none_of: must be a list of phrases")
        none_of = []

    binding = binding_kind = None
    if "account_binding" in spec:
        ident = obj("account_binding").get("identifier")
        if not isinstance(ident, dict):
            problems.append("account_binding: needs an identifier {pattern, transform, kind}")
        else:
            binding_kind = ident.get("kind")
            if binding_kind not in BINDING_KINDS:
                problems.append(f"account_binding: kind must be one of {', '.join(BINDING_KINDS)}")
            binding = _extractor("account_binding", ident, problems, pattern, required_transform=None)

    account_kind = spec.get("account_kind")
    if account_kind not in ACCOUNT_KINDS:
        problems.append(f"account_kind: must be one of {', '.join(ACCOUNT_KINDS)}")

    table_in = obj("table")
    rows = table_in.get("rows")
    columns: list[Column] = []
    header_contains: list[str] = []
    line_pattern = None
    fields: list[str] = []
    if rows == "ruled":
        cols = table_in.get("columns")
        if not (isinstance(cols, list) and cols):
            problems.append("table.columns: must list the columns left to right")
            cols = []
        for i, col in enumerate(cols):
            if not (isinstance(col, dict) and isinstance(col.get("label"), str) and col["label"].strip()):
                problems.append(f"table.columns[{i}]: needs a label (its header text)")
                continue
            field = col.get("field")
            if field is not None and field not in ROW_FIELDS:
                problems.append(f"table.columns[{i}]: unknown row field '{field}'")
            columns.append(Column(col["label"], field))
            header_contains.append(col["label"])
        fields = [c.field for c in columns if c.field]
    elif rows == "line_pattern":
        contains = table_in.get("header_row", {}).get("contains") if isinstance(table_in.get("header_row"), dict) else None
        if not (isinstance(contains, list) and contains and all(isinstance(s, str) and s for s in contains)):
            problems.append("table.header_row: needs contains: [the header line's labels]")
        else:
            header_contains = list(contains)
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
    ends_at = pattern("table.ends_at", table_in["ends_at"]) if "ends_at" in table_in else None
    resumes = pattern("table.resumes_after", table_in["resumes_after"]) if "resumes_after" in table_in else None

    rules = obj("row_rules")
    style = rules.get("amount_style")
    if style not in AMOUNT_STYLES:
        problems.append(f"row_rules.amount_style: must be one of {', '.join(AMOUNT_STYLES)}")
    unsigned = rules.get("unsigned_means")
    if style in ("signed_single_column", "cr_dr_suffix") and unsigned not in ("debit", "credit"):
        problems.append("row_rules.unsigned_means: debit or credit, for amounts with no sign or suffix")
    continuation = rules.get("continuation")
    if continuation not in CONTINUATIONS:
        problems.append(f"row_rules.continuation: must be one of {', '.join(CONTINUATIONS)}")
    skip = rules.get("skip_matching", [])
    skip_compiled = [pattern("row_rules.skip_matching", s) for s in skip] if isinstance(skip, list) else []
    stop_at = pattern("row_rules.stop_at", rules["stop_at"]) if "stop_at" in rules else None

    needed = ["date", "narration"] + (["debit", "credit"] if style == "separate_debit_credit" else ["amount"])
    if rows in ("ruled", "line_pattern"):
        for name in needed:
            if name not in fields:
                problems.append(f"table: rows need a '{name}' field for {style or 'this amount style'}")

    transforms = {}
    tf = obj("field_transforms")
    for name, tname in tf.items():
        if name not in ROW_FIELDS:
            problems.append(f"field_transforms.{name}: not a row field")
            continue
        family = _TYPED.get(name)
        if family and not (isinstance(tname, str) and tname.startswith(family)):
            problems.append(f"field_transforms.{name}: transform must be {family}{'<format>' if family.endswith(':') else ''}")
            continue
        try:
            transforms[name] = field_transform(tname)
        except (UnknownTransform, TypeError, AttributeError):
            problems.append(f"field_transforms.{name}: unknown transform '{tname}'")
    for name in fields:
        if name in _TYPED and name not in tf:
            problems.append(f"field_transforms.{name}: required, the row's {name} must convert to its type")

    declared = {}
    for name, raw in obj("declared_balances").items():
        if name not in ("opening", "closing"):
            problems.append(f"declared_balances.{name}: only opening and closing")
            continue
        ex = _extractor(f"declared_balances.{name}", raw, problems, pattern, required_transform="inr_to_paise")
        if ex:
            declared[name] = ex

    if problems:
        return None, problems
    return StatementSpec(
        detect_page=page, all_of=tuple(all_of), none_of=tuple(none_of), binding=binding, binding_kind=binding_kind,
        account_kind=account_kind,
        table=Table(rows, tuple(columns), tuple(header_contains), line_pattern, ends_at, resumes),
        amount_style=style, unsigned_means=unsigned, continuation=continuation,
        skip_matching=tuple(skip_compiled), stop_at=stop_at, transforms=transforms, declared=declared,
    ), problems


def _extractor(where: str, raw: object, problems: list[str], pattern, required_transform: str | None):
    if not isinstance(raw, dict):
        problems.append(f"{where}: must be {{pattern, transform}}")
        return None
    compiled = pattern(where, raw.get("pattern"), groups=1)
    tname = raw.get("transform")
    if required_transform and tname != required_transform:
        problems.append(f"{where}: transform must be {required_transform}")
        return None
    try:
        transform = field_transform(tname)
    except (UnknownTransform, TypeError, AttributeError):
        problems.append(f"{where}: unknown transform '{tname}'")
        return None
    return Extractor(compiled, transform) if compiled is not None else None
