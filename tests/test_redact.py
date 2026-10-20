"""Fixture redaction (tech-design §4.10 step 5, CLAUDE.md): a promoted format's examples are exported with
identifiers replaced by fake values of the same shape, consistently, so the template still matches them."""

import copy
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Any

from hisaab.parsing.email_interpreter import parse_eml
from hisaab.promotion.redact import redact
from hisaab.promotion.replay import Example, replay
from tests.gmail_fake import make_eml
from tests.promotion_helpers import ALERT, LANTERN, PEOPLE, alert_eml, reading
from tests.test_replay import SPEC

NAMES: list[str] = ["Asha Rao"]


def test_identifiers_are_gone_and_amount_and_date_stay() -> None:
    body: str = ALERT.format(verb="debited", **PEOPLE[0]) + "<p>Call 9876543210 or write to asha.rao@mail.example</p>"
    out, expected = redact(make_eml(LANTERN, "You have done a UPI txn", body), reading(0), NAMES)
    text: str = out.decode() + parse_eml(out).subject + parse_eml(out).body  # raw and decoded
    for secret in ("4321", "corner@okbank", "Corner Grocer", "612345678901", "Asha", "Rao", "asha@example.test",
                   "9876543210", "asha.rao@mail.example"):
        assert secret not in text, secret
    assert "1,250.00" in text and "05-10-26" in text and LANTERN in text
    assert (expected["amount"], expected["date"], expected["direction"]) == (125000, "2026-10-05", "debit")
    assert expected["account_identifier"] != "4321" and len(expected["account_identifier"]) == 4
    assert expected["account_identifier"] in text and expected["reference_number"] in text
    assert expected["payee_raw"] == "Xxxxxx Xxxxxx" and expected["counterparty_identifier"] == "xxxxxx@xxxxxx"


def test_only_from_subject_date_and_the_body_survive() -> None:
    out, _ = redact(alert_eml(0), reading(0), NAMES)
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(out)
    assert {k.lower() for k in msg.keys()} <= {"from", "subject", "date", "mime-version", "content-type",
                                               "content-transfer-encoding"}
    assert msg["From"] == LANTERN and msg["Subject"] == "You have done a UPI txn"


def test_the_same_value_gets_the_same_fake_everywhere() -> None:
    first, a = redact(alert_eml(0), reading(0), NAMES)
    second, b = redact(alert_eml(1), reading(1), NAMES)
    assert a["account_identifier"] == b["account_identifier"]  # one card, one fake
    assert a["reference_number"] != b["reference_number"]


def test_the_template_still_reads_the_redacted_copies() -> None:
    copies: list[Example] = []
    for n in range(3):
        out, expected = redact(alert_eml(n), reading(n), NAMES)
        copies.append(Example(f"s{n}", parse_eml(out), expected))
    result: dict[str, Any] = replay(copy.deepcopy(SPEC), copies)
    assert result["passed"], result["problems"]
