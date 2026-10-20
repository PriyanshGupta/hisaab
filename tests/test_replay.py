"""Replay validation (tech-design §4.10 step 3): a candidate runs over its three examples, and every field
must equal Haiku's cached reading exactly. It proves consistency, not correctness."""

import copy
from typing import Any

from hisaab.parsing.email_interpreter import parse_eml
from hisaab.promotion.replay import Example, replay
from tests.promotion_helpers import LANTERN, alert_eml, reading

SPEC: dict[str, Any] = {
    "version": 1,
    "match": {"sender": LANTERN, "subject": "^You have done a UPI txn"},
    "preprocess": ["strip_html", "collapse_whitespace"],
    "fields": {
        "amount": {"pattern": r"Rs\.([\d,]+\.\d{2}) has been debited", "transform": "inr_to_paise"},
        "currency": {"const": "INR"},
        "direction": {"const": "debit"},
        "account_identifier": {"pattern": r"from account \*\*(\d{4})"},
        "counterparty_identifier": {"pattern": r"to VPA (\S+)"},
        "payee_raw": {"pattern": r"to VPA \S+ (.+?) on \d"},
        "reference_number": {"pattern": r"reference number is (\d+)"},
        "date": {"pattern": r" on (\d{2}-\d{2}-\d{2})\.", "transform": "date:%d-%m-%y"},
        "time": {"required": False},
        "balance": {"required": False},
    },
}


def three() -> list[Example]:
    return [Example(f"s{n}", parse_eml(alert_eml(n)), reading(n)) for n in range(3)]


def with_field(name: str, field: dict[str, Any]) -> dict[str, Any]:
    spec: dict[str, Any] = copy.deepcopy(SPEC)
    spec["fields"][name] = field
    return spec


def test_a_faithful_candidate_passes_with_every_field_matching() -> None:
    result: dict[str, Any] = replay(SPEC, three())
    assert (result["passed"], result["problems"]) == (True, [])
    assert [e["source_id"] for e in result["examples"]] == ["s0", "s1", "s2"]
    rows: list[dict[str, Any]] = result["examples"][1]["fields"]
    assert [r["field"] for r in rows][:3] == ["outcome", "amount", "currency"]
    assert all(r["match"] for e in result["examples"] for r in e["fields"])
    assert rows[1] == {"field": "amount", "expected": 8950, "got": 8950, "match": True}


def test_a_field_read_differently_fails_and_names_the_example_and_field() -> None:
    wrong: dict[str, Any] = with_field("reference_number", {"pattern": r"reference number is (\d{6})"})
    result: dict[str, Any] = replay(wrong, three())
    assert result["passed"] is False
    assert "email 2: reference_number differs" in result["problems"]
    row: dict[str, Any] = next(r for r in result["examples"][1]["fields"] if r["field"] == "reference_number")
    assert row == {"field": "reference_number", "expected": "698765432109", "got": "698765", "match": False}


def test_a_subject_pattern_that_misses_an_example_fails() -> None:
    spec: dict[str, Any] = copy.deepcopy(SPEC)
    spec["match"]["subject"] = "^You have done a UPI transaction"
    result: dict[str, Any] = replay(spec, three())
    assert result["passed"] is False and "email 1: the sender or subject pattern doesn't match" in result["problems"]


def test_a_critical_field_that_fails_means_the_template_does_not_match() -> None:
    result: dict[str, Any] = replay(with_field("amount", {"pattern": r"USD ([\d.]+)", "transform": "inr_to_paise"}),
                                    three())
    assert result["passed"] is False
    assert [e["matched"] for e in result["examples"]] == [False, False, False]
    assert "email 1: amount differs" in result["problems"]


def test_an_invalid_spec_is_refused_before_replay() -> None:
    result: dict[str, Any] = replay(with_field("payee_raw", {"pattern": "(" + "a" * 600 + ")"}), three())
    assert result["passed"] is False and result["examples"] == []
    assert any("payee_raw" in p for p in result["problems"])
    assert replay(with_field("amount", {"pattern": r"Rs\.([\d,.]+)", "transform": "no_such"}), three())["passed"] is False


def test_times_compare_to_the_minute_because_haiku_reads_hh_mm_and_templates_hh_mm_ss() -> None:
    spec: dict[str, Any] = copy.deepcopy(SPEC)
    spec["fields"]["time"] = {"pattern": r"at (\d{2}:\d{2}:\d{2})", "transform": "time:%H:%M:%S"}
    from tests.gmail_fake import make_eml
    from tests.promotion_helpers import ALERT, PEOPLE
    examples: list[Example] = []
    for n in range(3):
        html: str = ALERT.format(verb="debited", **PEOPLE[n]).replace("</table>", f"<p>at 09:1{n}:4{n}</p></table>")
        examples.append(Example(f"s{n}", parse_eml(make_eml(LANTERN, "You have done a UPI txn", html)),
                                {**reading(n), "time": f"09:1{n}"}))
    result: dict[str, Any] = replay(spec, examples)
    assert result["passed"], result["problems"]
    wrong: list[Example] = [Example(e.source_id, e.artifact, {**e.expected, "time": "10:00"}) for e in examples]
    assert "email 1: time differs" in replay(spec, wrong)["problems"]
