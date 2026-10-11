"""The ICICI card-purchase seed spec against the real fixtures (authored with try-template, slice 9).
Fixture values never appear here: emails and hand-read expected contracts are read at runtime."""

import json
from importlib.resources import files

import pytest

from hisaab.config import PROJECT_ROOT
from hisaab.parsing.email_interpreter import EmailTemplate, interpret, parse_eml
from hisaab.parsing.email_spec import parse_email_spec

CARDS = PROJECT_ROOT / "fixtures" / "emails" / "credit_cards"
pytestmark = pytest.mark.skipif(not CARDS.is_dir(), reason="fixtures are local-only (gitignored)")
SPEC = parse_email_spec(json.loads((files("hisaab.parsing") / "seed" / "icici_cc_purchase.json").read_text()))


def run(name):
    artifact = parse_eml((CARDS / f"{name}.eml").read_bytes())
    return artifact, interpret([EmailTemplate("icici", "ICICI card purchase", SPEC)], artifact)


@pytest.mark.parametrize("name", ["icici_cc_purchase_01", "icici_cc_purchase_02"])
def test_every_field_of_both_purchase_alerts(name):
    artifact, result = run(name)
    assert artifact.sender in SPEC.senders and SPEC.subject.search(artifact.subject)
    assert result.extraction == json.loads((CARDS / f"{name}.expected.json").read_text())


def test_the_two_fixtures_come_from_the_old_and_new_sender():
    senders = {run(n)[0].sender for n in ("icici_cc_purchase_01", "icici_cc_purchase_02")}
    assert senders == set(SPEC.senders)


def test_the_repayment_alert_is_not_taken_for_a_purchase():
    artifact, result = run("icici_cc_repayment_01")
    assert not SPEC.subject.search(artifact.subject)  # never even a candidate
    assert result.extraction is None  # and it would decline if it were
