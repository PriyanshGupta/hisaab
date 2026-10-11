"""Arithmetic validation (tech-design §4.8): passed, failed with the gap, or unavailable. Never inferred."""

import copy
from typing import Any

from hisaab.parsing.pdf import read_pdf
from hisaab.parsing.statement_interpreter import interpret_statement
from hisaab.parsing.statement_spec import parse_statement_spec
from tests.pdf_factory import PageSpec, make_pdf
from tests.statement_samples import card_page, ruled_page
from tests.test_statement_interpreter import ROWS
from tests.test_statement_spec import LINES, RULED


def summary(closing: str) -> list[str]:
    return ["STATEMENT SUMMARY", "Opening 1,000.00", f"Closing {closing}"]


def validation(spec: dict[str, Any], page: PageSpec, **kw: Any) -> dict[str, Any]:
    return interpret_statement(parse_statement_spec(spec), read_pdf(make_pdf([page])), **kw).validation


def test_a_statement_whose_rows_add_up_passes() -> None:
    v: dict[str, Any] = validation(RULED, ruled_page(ROWS, first=True, summary=["STATEMENT SUMMARY", "Opening 1,000.00",
                                                                "Closing 1,450.00"]),
                   template_id="t-1", template_name="Example savings")
    assert v == {
        "outcome": "passed",
        "template_id": "t-1", "template_name": "Example savings",
        "pages_processed": 1, "rows_extracted": 3, "rows_skipped": 0, "rows_unparseable": 0,
        "declared": {"opening": 100000, "closing": 145000},
        "computed": {"credits": 50000, "debits": 5000, "closing": 145000},
        "gap_paise": 0,
        "unparseable_rows": [],
    }


def test_rows_that_do_not_add_up_fail_with_the_gap_declared_minus_computed() -> None:
    v: dict[str, Any] = validation(RULED, ruled_page(ROWS, first=True, summary=summary("1,550.00")))
    assert (v["outcome"], v["computed"]["closing"], v["gap_paise"]) == ("failed", 145000, 10000)
    assert v["rows_unparseable"] == 0


def test_the_tolerance_is_one_rupee_inclusive() -> None:
    within: dict[str, Any] = validation(RULED, ruled_page(ROWS, first=True, summary=summary("1,451.00")))
    beyond: dict[str, Any] = validation(RULED, ruled_page(ROWS, first=True, summary=summary("1,451.01")))
    assert (within["outcome"], within["gap_paise"]) == ("passed", 100)
    assert (beyond["outcome"], beyond["gap_paise"]) == ("failed", 101)


def test_no_declared_balances_in_the_layout_is_unavailable_even_with_a_running_balance_column() -> None:
    spec: dict[str, Any] = copy.deepcopy(RULED)
    del spec["declared_balances"]
    v: dict[str, Any] = validation(spec, ruled_page(ROWS, first=True))  # every row carries a balance; none of it is used
    assert v["outcome"] == "unavailable"
    assert v["declared"] == {"opening": None, "closing": None}
    assert v["computed"] == {"credits": 50000, "debits": 5000, "closing": None}
    assert v["gap_paise"] is None


def test_a_declared_balance_the_statement_does_not_show_is_unavailable_not_failed() -> None:
    v: dict[str, Any] = validation(RULED, ruled_page(ROWS, first=True, summary=["Opening 1,000.00"]))  # no closing line
    assert (v["outcome"], v["declared"]) == ("unavailable", {"opening": 100000, "closing": None})


def test_a_card_statement_adds_debits_and_subtracts_credits() -> None:
    lines: list[tuple[str, str, bool]] = [("17/07/2026| 10:00 SHOP", "500.00", False), ("18/07/2026| 11:00 SHOP", "200.00", False),
             ("19/07/2026| 12:00 PAYMENT", "250.00", True)]
    v: dict[str, Any] = validation(LINES, card_page(lines, first=True))  # previous 1,000 + 700 - 250 = 1,450
    assert (v["outcome"], v["computed"]) == ("passed", {"credits": 25000, "debits": 70000, "closing": 145000})
    as_bank: dict[str, Any] = copy.deepcopy(LINES)
    as_bank["account_kind"] = "bank"
    assert validation(as_bank, card_page(lines, first=True))["outcome"] == "failed"


def test_unparseable_rows_are_listed_in_the_diagnostic() -> None:
    rows: list[tuple[str, str, str, str, str, str]] = [*ROWS, ("04/07/26", "ODD", "9", "5.00", "5.00", "")]
    v: dict[str, Any] = validation(RULED, ruled_page(rows, first=True, summary=summary("1,450.00")))
    assert v["rows_unparseable"] == 1
    assert v["unparseable_rows"] == [{"page": 1, "raw_cells": ["04/07/26", "ODD", "9", "5.00", "5.00", ""],
                                      "reason": "both debit and credit are filled"}]
