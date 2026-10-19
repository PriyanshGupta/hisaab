"""The §4.3 interpreter loop, on synthetic alerts (no fixture content)."""

import copy
from collections.abc import Callable
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

import pytest
from re2 import _Match, _Regexp

from hisaab.parsing import patterns
from hisaab.parsing.email_interpreter import (
    Attempt,
    EmailArtifact,
    EmailTemplate,
    FieldResult,
    Interpretation,
    interpret,
    parse_eml,
)
from hisaab.parsing.email_spec import parse_email_spec
from tests.test_email_spec import SPEC

BODY: str = (
    "<html><head><style>td{}</style></head><body><table><tr><td>"
    "Rs.&nbsp;1,234.50 debited from account ending 1111 to VPA Shop.Owner@OKBANK (Corner Shop) "
    "on 05-10-26. Ref 9988776655</td></tr></table></body></html>"
)
RECEIVED: datetime = datetime(2026, 10, 5, 20, 0, tzinfo=UTC)  # 01:30 on 6 Oct in India


def artifact(body: str = BODY, received_at: datetime = RECEIVED) -> EmailArtifact:
    return EmailArtifact(sender="alerts@example-bank.in", subject="Debit alert", body=body, received_at=received_at)


def template(spec: dict[str, Any] = SPEC, id: str = "t-1", name: str = "Example debit") -> EmailTemplate:
    return EmailTemplate(id=id, name=name, spec=parse_email_spec(spec))


def spec_with(**fields: dict[str, Any]) -> dict[str, Any]:
    spec: dict[str, Any] = copy.deepcopy(SPEC)
    spec["fields"].update(fields)
    return spec


def test_a_matching_template_emits_the_full_contract() -> None:
    result: Interpretation = interpret([template()], artifact())
    assert result.template_id == "t-1"
    assert result.extraction == {
        "outcome": "transaction",
        "amount": 123450,
        "currency": "INR",
        "direction": "debit",
        "account_identifier": "1111",
        "counterparty_identifier": "shop.owner@okbank",
        "payee_raw": "Corner Shop",
        "reference_number": "9988776655",
        "date": "2026-10-05",
        "time": None,
        "balance": None,
        "missing_fields": [],
    }
    assert not result.needs_review
    assert [a.matched for a in result.attempts] == [True]


def test_an_optional_field_that_is_absent_is_null_and_not_missing() -> None:
    result: Interpretation = interpret([template()], artifact(BODY.replace("to VPA Shop.Owner@OKBANK ", "")))
    assert result.extraction is not None
    assert result.extraction["counterparty_identifier"] is None
    assert result.extraction["missing_fields"] == []


def test_a_required_non_critical_field_that_fails_sends_the_transaction_to_review() -> None:
    result: Interpretation = interpret([template()], artifact(BODY.replace("Ref 9988776655", "")))
    assert result.extraction is not None
    assert result.extraction["reference_number"] is None
    assert result.extraction["missing_fields"] == ["reference_number"]
    assert result.needs_review


def test_an_empty_capture_counts_as_a_failure_not_an_empty_string() -> None:
    result: Interpretation = interpret([template()], artifact(BODY.replace("(Corner Shop)", "()")))
    # payee pattern \((.+?)\) can't match "()", so the field fails; with "( )" trim would leave ""
    assert result.extraction is not None
    assert result.extraction["missing_fields"] == ["payee_raw"]
    blank: Interpretation = interpret([template()], artifact(BODY.replace("(Corner Shop)", "( )")))
    assert blank.extraction is not None
    assert blank.extraction["payee_raw"] is None and blank.extraction["missing_fields"] == ["payee_raw"]


def test_a_failing_critical_field_means_the_template_does_not_match_and_the_next_one_runs() -> None:
    stale: EmailTemplate = template(spec_with(amount={"pattern": r"INR ([\d,.]+) spent", "transform": "inr_to_paise"}),
                     id="stale", name="Stale debit")
    result: Interpretation = interpret([stale, template()], artifact())
    assert result.template_id == "t-1"
    assert result.extraction is not None
    assert result.extraction["amount"] == 123450
    first: Attempt = result.attempts[0]
    assert (first.template_id, first.matched) == ("stale", False)
    assert [f.field for f in first.failures] == ["amount"]


def test_exhausting_the_candidates_emits_nothing() -> None:
    stale: EmailTemplate = template(spec_with(amount={"pattern": r"INR ([\d,.]+) spent", "transform": "inr_to_paise"}))
    result: Interpretation = interpret([stale], artifact())
    assert result.extraction is None and result.template_id is None
    assert not result.needs_review
    assert interpret([], artifact()).extraction is None


def test_a_critical_transform_that_raises_also_declines() -> None:
    result: Interpretation = interpret([template()], artifact(BODY.replace("05-10-26", "31-02-26")))
    assert result.extraction is None
    assert [(f.field, f.reason) for f in result.attempts[0].failures] == [("date", "not a date in the form %d-%m-%y")]


def test_account_identifier_is_critical_too() -> None:
    result: Interpretation = interpret([template()], artifact(BODY.replace("account ending 1111", "your account")))
    assert result.extraction is None
    assert result.attempts[0].failures[0].field == "account_identifier"


def test_failure_reasons_never_repeat_the_captured_text() -> None:
    result: Interpretation = interpret([template(spec_with(amount={"pattern": r"Rs\.\s*(\S+) debited", "transform": "inr_to_paise"}))],
                       artifact(BODY.replace("1,234.50", "12,34,5.0")))
    (failure,) = result.attempts[0].failures
    assert "12,34" not in failure.reason


def test_a_format_that_never_states_a_date_uses_the_received_time_in_india() -> None:
    result: Interpretation = interpret([template(spec_with(date={"required": False}))], artifact())
    assert result.extraction is not None
    assert result.extraction["date"] == "2026-10-06"  # 20:00 UTC on the 5th is the 6th in India


def test_a_pattern_that_times_out_fails_its_field(monkeypatch: pytest.MonkeyPatch) -> None:
    real: Callable[[_Regexp, str, float | None], _Match | None] = patterns.search

    def slow_for_references(
        compiled: patterns.Pattern, text: str, timeout: float | None = None,
    ) -> patterns.Match | None:
        if "Ref" in compiled.pattern:
            raise patterns.PatternTimeout("pattern timed out")
        return real(compiled, text, timeout)

    monkeypatch.setattr(patterns, "search", slow_for_references)
    result: Interpretation = interpret([template()], artifact())
    assert result.extraction is not None
    assert result.extraction["missing_fields"] == ["reference_number"]
    assert result.attempts[0].failures[0].reason == "pattern timed out"


def test_preprocessing_runs_in_the_order_listed() -> None:
    raw_spec: dict[str, Any] = copy.deepcopy(SPEC)
    raw_spec["preprocess"] = []  # patterns then see the HTML, where "Rs.&nbsp;" breaks the amount
    assert interpret([template(raw_spec)], artifact()).extraction is None


# --- reading an .eml (the shape Gmail's raw format returns) -----------------------------

def test_parse_eml_reads_sender_subject_html_body_and_date() -> None:
    msg: EmailMessage[Any, Any] = EmailMessage()
    msg["From"] = "Example Bank Alerts <Alerts@Example-Bank.in>"
    msg["Subject"] = "=?utf-8?q?=E2=9D=97_Debit_alert?="
    msg["Date"] = "Mon, 05 Oct 2026 20:00:00 +0000"
    msg.set_content("plain fallback")
    msg.add_alternative(BODY, subtype="html")
    parsed: EmailArtifact = parse_eml(msg.as_bytes())
    assert parsed.sender == "alerts@example-bank.in"
    assert parsed.subject == "❗ Debit alert"
    assert "1,234.50" in parsed.body and "<table>" in parsed.body  # the HTML part is preferred
    assert parsed.received_at == RECEIVED


def test_parse_eml_falls_back_to_plain_text() -> None:
    msg: EmailMessage[Any, Any] = EmailMessage()
    msg["From"] = "alerts@example-bank.in"
    msg["Subject"] = "Debit alert"
    msg["Date"] = "Mon, 05 Oct 2026 20:00:00 +0000"
    msg.set_content("Rs.1,234.50 debited")
    assert parse_eml(msg.as_bytes()).body.strip() == "Rs.1,234.50 debited"



# --- trace: the per-field record the authoring harness prints ---------------------------

def test_trace_shows_the_preprocessed_text_and_each_fields_capture_and_value() -> None:
    from hisaab.parsing.email_interpreter import trace

    text, results = trace(parse_email_spec(SPEC), artifact(BODY.replace("Ref 9988776655", "")))
    assert text.startswith("Rs. 1,234.50 debited") or text.startswith("Rs. 1,234.50 debited")
    by: dict[str, FieldResult] = {r.name: r for r in results}
    assert (by["amount"].captured, by["amount"].value, by["amount"].reason) == ("1,234.50", 123450, None)
    assert (by["reference_number"].captured, by["reference_number"].reason) == (None, "no match")
    assert by["direction"].value == "debit" and by["direction"].captured is None
    assert by["time"].required is False


def test_trace_keeps_the_capture_when_the_transform_rejects_it() -> None:
    from hisaab.parsing.email_interpreter import trace

    _, results = trace(parse_email_spec(SPEC), artifact(BODY.replace("05-10-26", "31-02-26")))
    date: FieldResult = next(r for r in results if r.name == "date")
    assert (date.captured, date.value, date.reason) == ("31-02-26", None, "not a date in the form %d-%m-%y")


# --- non-transactional templates ---------------------------------------------------------

NOTICE_BODY: str = ("<p>There is an upcoming E-mandate (Auto payment) of INR 100.00 for STREAMCO. Amount will be "
                    "debited from your Credit Card ending 4321 on 16/09/2026.</p>")


def notice_template() -> EmailTemplate:
    from tests.test_email_spec import NOTICE

    return EmailTemplate(id="notice", name="Example mandate notice", spec=parse_email_spec(NOTICE))


def test_a_notice_that_confirms_is_classified_non_transactional() -> None:
    result: Interpretation = interpret([notice_template()], artifact(NOTICE_BODY))
    assert result.template_id == "notice"
    assert result.extraction is not None and result.extraction["outcome"] == "non_transactional"
    assert result.extraction["amount"] is None and result.extraction["missing_fields"] == []
    assert not result.needs_review


def test_a_notice_template_declines_a_real_transaction_and_vice_versa() -> None:
    # same sender and subject in real life (HDFC credit alert vs e-mandate notice)
    on_debit: Interpretation = interpret([notice_template(), template()], artifact())
    assert on_debit.template_id == "t-1" and not on_debit.attempts[0].matched
    on_notice: Interpretation = interpret([template(), notice_template()], artifact(NOTICE_BODY))
    assert on_notice.template_id == "notice" and not on_notice.attempts[0].matched


PAID_BODY: str = ("<p>Dear Customer, Greetings from HDFC Bank! Your STREAMCO bill, set up through E-mandate (Auto "
                  "payment), has been successfully paid using your HDFC Bank Credit Card ending 4321. Transaction "
                  "Details: Amount: INR 100.00 Date: 16/09/2026 Mandate ID: AbC123xYz9</p>")


def seed_template(name: str) -> EmailTemplate:
    import json

    from hisaab.seeds import SEED_TEMPLATES

    return EmailTemplate(id=name, name=name, spec=parse_email_spec(json.loads((SEED_TEMPLATES / f"{name}.json").read_text())))


@pytest.mark.parametrize("id_label", ["Mandate ID", "SI Hub ID"])  # HDFC's newer and older wording
def test_an_e_mandate_payment_confirmation_records_nothing_its_charge_alert_is_the_record(id_label: str) -> None:
    """HDFC confirms each card auto-payment after a separate charge alert for the same money. Recording the
    confirmation too counted every subscription twice, and its Mandate ID (the same every month) merged
    months of confirmations into one phantom charge (user, 2026-10-19)."""
    paid: EmailArtifact = EmailArtifact(sender="alerts@hdfcbank.bank.in", subject="View: Account update for your HDFC Bank A/c",
                                        body=PAID_BODY.replace("Mandate ID", id_label), received_at=datetime(2026, 9, 16, tzinfo=UTC))
    result: Interpretation = interpret(
        [seed_template("hdfc_upi_credit"), seed_template("hdfc_cc_mandate"), seed_template("hdfc_cc_mandate_paid")], paid)
    assert result.template_id == "hdfc_cc_mandate_paid"
    assert result.extraction is not None and result.extraction["outcome"] == "non_transactional"
