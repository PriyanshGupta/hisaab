"""Email template specs are validated when saved (tech-design §4.2, §4.5): a bad spec never gets stored."""

import copy

import pytest

from hisaab.parsing.email_spec import CONTRACT_FIELDS, CRITICAL_FIELDS, InvalidSpec, parse_email_spec

SPEC = {
    "version": 1,
    "match": {"sender": "Alerts@Example-Bank.in", "subject": "^Debit alert"},
    "preprocess": ["strip_html", "collapse_whitespace"],
    "fields": {
        "amount": {"pattern": r"Rs\.\s*([\d,]+\.\d{2}) debited", "transform": "inr_to_paise"},
        "direction": {"const": "debit"},
        "currency": {"const": "INR"},
        "account_identifier": {"pattern": r"account ending (\d{4})", "transform": "digits_only"},
        "counterparty_identifier": {"pattern": r"to VPA (\S+)", "transform": "normalize_vpa", "required": False},
        "payee_raw": {"pattern": r"\((.+?)\)", "transform": "trim"},
        "reference_number": {"pattern": r"Ref (\d+)"},
        "date": {"pattern": r"on (\d{2}-\d{2}-\d{2})", "transform": "date:%d-%m-%y"},
        "time": {"required": False},
        "balance": {"required": False},
    },
}


def with_field(name, value):
    spec = copy.deepcopy(SPEC)
    if value is None:
        del spec["fields"][name]
    else:
        spec["fields"][name] = value
    return spec


def problems(spec):
    with pytest.raises(InvalidSpec) as e:
        parse_email_spec(spec)
    return e.value.problems


def test_a_valid_spec_parses():
    parsed = parse_email_spec(SPEC)
    assert parsed.sender == "alerts@example-bank.in"  # addresses compare case-insensitively
    assert [f.name for f in parsed.fields] == list(CONTRACT_FIELDS)
    assert CRITICAL_FIELDS == ("amount", "date", "account_identifier")
    amount = next(f for f in parsed.fields if f.name == "amount")
    assert amount.required and amount.transform("Rs. 1,000.00") == 100000


def test_contract_fields_match_prd_10_4():
    assert set(CONTRACT_FIELDS) == {"amount", "currency", "direction", "account_identifier",
                                    "counterparty_identifier", "payee_raw", "reference_number",
                                    "date", "time", "balance"}


@pytest.mark.parametrize(
    ("spec", "fragment"),
    [
        ({**SPEC, "version": 2}, "version"),
        ({**SPEC, "match": {"subject": "x"}}, "match.sender"),
        ({**SPEC, "match": {"sender": "not-an-address", "subject": "x"}}, "match.sender"),
        ({**SPEC, "match": {"sender": "a@b.in", "subject": "(unclosed"}}, "match.subject"),
        ({**SPEC, "preprocess": ["strip_html", "lowercase"]}, "lowercase"),
        (with_field("balance", None), "balance: not declared"),
        (with_field("category", {"const": "Food"}), "category: not a contract field"),
        (with_field("payee_raw", {"pattern": r"(\w+)", "regex": "x"}), "payee_raw: unknown key 'regex'"),
        (with_field("payee_raw", {"pattern": r"(\w+)", "const": "x"}), "payee_raw: both const and pattern"),
        (with_field("payee_raw", {"const": "x", "transform": "trim"}), "payee_raw: transform needs a pattern"),
        (with_field("payee_raw", {"pattern": r"\w+"}), "payee_raw: pattern needs a capture group"),
        (with_field("payee_raw", {"pattern": "(?=x)(a)"}), "payee_raw: RE2 cannot compile"),
        (with_field("payee_raw", {"pattern": r"(\w+)", "transform": "titlecase"}), "payee_raw: unknown transform"),
        (with_field("payee_raw", {}), "payee_raw: no pattern or const"),
        (with_field("payee_raw", {"pattern": r"(\w+)", "required": "yes"}), "payee_raw: required must be true or false"),
        (with_field("amount", {"required": False}), "amount: critical"),
        (with_field("amount", {"pattern": r"([\d.]+)", "transform": "inr_to_paise", "required": False}), "amount: critical"),
        (with_field("account_identifier", {"const": "1234"}), "account_identifier: critical"),
        (with_field("date", {"pattern": r"(\S+)", "transform": "date:%d-%m-%y", "required": False}), "date: critical"),
        (with_field("amount", {"pattern": r"([\d.]+)"}), "amount: transform must be inr_to_paise"),
        (with_field("balance", {"pattern": r"([\d.]+)", "transform": "digits_only"}), "balance: transform must be inr_to_paise"),
        (with_field("date", {"pattern": r"(\S+)", "transform": "time:%H:%M"}), "date: transform must be date:"),
        (with_field("time", {"pattern": r"(\S+)", "required": False}), "time: transform must be time:"),
        (with_field("direction", {"const": "out"}), "direction: const must be debit or credit"),
        (with_field("currency", {"const": "rupees"}), "currency: const must be a 3-letter code"),
    ],
)
def test_each_rule_names_the_offending_part(spec, fragment):
    assert any(fragment in p for p in problems(spec)), problems(spec)


def test_date_may_be_declared_absent_and_then_falls_back_to_received_time():
    parsed = parse_email_spec(with_field("date", {"required": False}))
    date = next(f for f in parsed.fields if f.name == "date")
    assert date.pattern is None and not date.required


def test_all_problems_are_reported_together():
    spec = copy.deepcopy(SPEC)
    spec["version"] = 3
    spec["fields"]["direction"] = {"const": "sideways"}
    del spec["fields"]["time"]
    found = problems(spec)
    assert len(found) == 3


def test_a_non_object_spec_is_refused():
    assert problems([]) == ["spec must be a JSON object"]
    assert any("fields" in p for p in problems({**SPEC, "fields": []}))
