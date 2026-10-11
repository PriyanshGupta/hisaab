"""Statement template specs (tech-design §4.6, amended 2026-10-11: two row modes) and save-time validation."""

import copy
from typing import Any

import pytest

from hisaab.parsing.statement_spec import (
    InvalidStatementSpec,
    StatementSpec,
    check_statement_spec,
    parse_statement_spec,
)

RULED: dict[str, Any] = {
    "version": 1,
    "artifact_type": "statement",
    "detect": {"page": 1, "all_of": ["Example Bank", "Statement of account"], "none_of": ["Credit Card"]},
    "account_binding": {"identifier": {"pattern": r"Account No\s*:\s*\d*(\d{4})\b", "transform": "digits_only",
                                       "kind": "last4"}},
    "account_kind": "bank",
    "table": {
        "rows": "ruled",
        "columns": [{"label": "Date", "field": "date"}, {"label": "Narration", "field": "narration"},
                    {"label": "Ref", "field": "reference"}, {"label": "Withdrawal", "field": "debit"},
                    {"label": "Deposit", "field": "credit"}, {"label": "Balance", "field": "balance"}],
        "ends_at": "^Example Bank Ltd",
    },
    "row_rules": {"amount_style": "separate_debit_credit", "continuation": "empty_date_column",
                  "skip_matching": ["^Brought forward"], "stop_at": "^STATEMENT SUMMARY"},
    "field_transforms": {"date": "date:%d/%m/%y", "debit": "inr_to_paise", "credit": "inr_to_paise",
                         "balance": "inr_to_paise"},
    "declared_balances": {
        "opening": {"pattern": r"Opening\s+([\d,]+\.\d{2})", "transform": "inr_to_paise"},
        "closing": {"pattern": r"Closing\s+([\d,]+\.\d{2})", "transform": "inr_to_paise"},
    },
}

LINES: dict[str, Any] = {
    "version": 1,
    "artifact_type": "statement",
    "detect": {"all_of": ["Example Card Statement"]},
    "account_binding": {"identifier": {"pattern": r"Card No\.\s*\d{4}X+(\d{4})", "transform": "digits_only",
                                       "kind": "card_last4"}},
    "account_kind": "credit_card",
    "table": {
        "rows": "line_pattern",
        "header_row": {"contains": ["DATE", "DESCRIPTION", "AMOUNT"]},
        "line_pattern": r"^(?P<date>\d{2}/\d{2}/\d{4})\s*\|\s*(?P<time>\d{2}:\d{2})\s+(?P<narration>.+?)\s+"
                        r"(?P<amount>\+?\s*₹\s*[\d,]+\.\d{2})$",
        "ends_at": r"^Page \d+ of \d+",
    },
    "row_rules": {"amount_style": "signed_single_column", "unsigned_means": "debit", "continuation": "none",
                  "skip_matching": [r"\[CKYC"]},
    "field_transforms": {"date": "date:%d/%m/%Y", "time": "time:%H:%M", "amount": "inr_to_paise"},
    "declared_balances": {
        "opening": {"pattern": r"Previous Dues\s+₹\s*([\d,]+\.\d{2})", "transform": "inr_to_paise"},
        "closing": {"pattern": r"Total Due\s+₹\s*([\d,]+\.\d{2})", "transform": "inr_to_paise"},
    },
}


def changed(spec: dict[str, Any], path: str, value: object) -> dict[str, Any]:
    spec = copy.deepcopy(spec)
    *parents, last = path.split(".")
    node: dict[str, Any] = spec
    for key in parents:
        node = node[key]
    if value is None:
        node.pop(last, None)
    else:
        node[last] = value
    return spec


def problems(spec: object) -> list[str]:
    with pytest.raises(InvalidStatementSpec) as e:
        parse_statement_spec(spec)
    return e.value.problems


def test_both_row_modes_parse() -> None:
    ruled: StatementSpec = parse_statement_spec(RULED)
    assert ruled.table.rows == "ruled" and [c.field for c in ruled.table.columns][:2] == ["date", "narration"]
    lines: StatementSpec = parse_statement_spec(LINES)
    assert lines.table.rows == "line_pattern" and lines.account_kind == "credit_card"
    assert lines.detect_page == 1  # default


def test_declared_balances_are_optional() -> None:
    assert parse_statement_spec(changed(RULED, "declared_balances", None)).declared == {}


@pytest.mark.parametrize(
    ("spec", "fragment"),
    [
        (changed(RULED, "version", 2), "version"),
        (changed(RULED, "artifact_type", "email"), "artifact_type"),
        (changed(RULED, "detect.all_of", []), "detect.all_of"),
        (changed(RULED, "detect.page", 0), "detect.page"),
        (changed(RULED, "account_kind", "wallet"), "account_kind"),
        (changed(RULED, "account_binding.identifier.kind", "vpa"), "account_binding"),
        (changed(RULED, "account_binding.identifier.pattern", r"\d{4}"), "account_binding"),
        (changed(RULED, "table.rows", "grid"), "table.rows"),
        (changed(RULED, "table.columns", []), "table.columns"),
        (changed(RULED, "table.columns", [{"label": "Date", "field": "date"}, {"label": "X", "field": "date"},
                                          {"label": "N", "field": "narration"}]), "more than once"),
        (changed(RULED, "table.columns", [{"label": "Date", "field": "when"}]), "unknown row field 'when'"),
        (changed(RULED, "table.ends_at", "(?=x)"), "table.ends_at"),
        (changed(LINES, "table.line_pattern", r"^(?P<date>\S+) (?P<flavour>.+)$"), "unknown row field 'flavour'"),
        (changed(LINES, "table.header_row", None), "table.header_row"),
        (changed(RULED, "row_rules.amount_style", "magic"), "amount_style"),
        (changed(LINES, "row_rules.unsigned_means", None), "unsigned_means"),
        (changed(RULED, "row_rules.continuation", "glue"), "continuation"),
        (changed(RULED, "row_rules.skip_matching", ["(unclosed"]), "skip_matching"),
        (changed(RULED, "field_transforms.date", "trim"), "date: transform must be date:"),
        (changed(RULED, "field_transforms.debit", "digits_only"), "debit: transform must be inr_to_paise"),
        (changed(RULED, "field_transforms.colour", "trim"), "colour"),
        (changed(RULED, "declared_balances.middle", {"pattern": "(x)", "transform": "inr_to_paise"}), "middle"),
        (changed(RULED, "declared_balances.opening", {"pattern": "x", "transform": "inr_to_paise"}), "declared_balances.opening"),
    ],
)
def test_each_rule_names_the_offending_part(spec: dict[str, Any], fragment: str) -> None:
    found: list[str] = problems(spec)
    assert any(fragment in p for p in found), found


def test_the_amount_style_decides_which_fields_the_rows_need() -> None:
    # separate_debit_credit needs debit and credit columns; a single-column style needs amount
    found: list[str] = problems(changed(RULED, "row_rules.amount_style", "cr_dr_suffix") | {})
    assert any("amount" in p for p in found)
    no_credit: dict[str, Any] = copy.deepcopy(RULED)
    no_credit["table"]["columns"] = [c for c in RULED["table"]["columns"] if c["field"] != "credit"]
    assert any("credit" in p for p in problems(no_credit))
    no_date: dict[str, Any] = changed(LINES, "table.line_pattern", r"^(?P<narration>.+?)\s+(?P<amount>\S+)$")
    assert any("date" in p for p in problems(no_date))


def test_problems_are_collected_not_first_only() -> None:
    spec: dict[str, Any] = changed(changed(RULED, "version", 9), "account_kind", "wallet")
    _, found = check_statement_spec(spec)
    assert len(found) == 2
