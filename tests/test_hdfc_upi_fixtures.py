"""The two HDFC UPI seed specs against the real fixture emails (build-plan slice 8 verification).

Fixtures are unredacted and local-only (gitignored), so nothing from them appears
here: the emails and their hand-read expected contracts (<name>.expected.json,
beside each .eml) are read at runtime, and the tests skip on a checkout without them.
"""

import copy
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from hisaab.config import PROJECT_ROOT
from hisaab.db import Database
from hisaab.parsing import templates
from hisaab.parsing.email_interpreter import EmailArtifact, EmailTemplate, Interpretation, interpret, parse_eml
from hisaab.parsing.email_spec import parse_email_spec

EMAILS: Path = PROJECT_ROOT / "fixtures" / "emails"
pytestmark: pytest.MarkDecorator = pytest.mark.skipif(not (EMAILS / "upi").is_dir(), reason="fixtures are local-only (gitignored)")


def seed(name: str) -> dict[str, Any]:
    spec: dict[str, Any] = json.loads((files("hisaab.parsing") / "seed" / f"{name}.json").read_text())
    return spec


def email(path: str) -> EmailArtifact:
    return parse_eml((EMAILS / f"{path}.eml").read_bytes())


def expected(path: str) -> dict[str, Any]:
    contract: dict[str, Any] = json.loads((EMAILS / f"{path}.expected.json").read_text())
    return contract


def tmpl(spec: dict[str, Any], id: str) -> EmailTemplate:
    return EmailTemplate(id=id, name=id, spec=parse_email_spec(spec))


DEBIT, CREDIT = "upi/hdfc_upi_debit_01", "upi/hdfc_upi_credit_01"
MANDATE: str = "credit_cards/hdfc_cc_mandate_01"


@pytest.mark.parametrize(("spec", "fixture"), [("hdfc_upi_debit", DEBIT), ("hdfc_upi_credit", CREDIT)])
def test_each_spec_extracts_every_field_of_its_fixture(spec: str, fixture: str) -> None:
    result: Interpretation = interpret([tmpl(seed(spec), spec)], email(fixture))
    assert result.extraction == expected(fixture)
    assert result.extraction["missing_fields"] == []


def test_the_specs_match_their_fixtures_subject_and_sender() -> None:
    for spec, fixture in (("hdfc_upi_debit", DEBIT), ("hdfc_upi_credit", CREDIT)):
        parsed, artifact = parse_email_spec(seed(spec)), email(fixture)
        assert artifact.sender in parsed.senders
        assert parsed.subject is not None and parsed.subject.search(artifact.subject)



def route(db: Database, artifact: EmailArtifact) -> Interpretation:
    with db.read_session() as s:
        return interpret(templates.email_candidates(s, artifact.sender, artifact.subject), artifact)


def test_with_both_templates_active_each_email_reaches_its_own(db: Database) -> None:
    with db.write_session() as s:
        debit: str = templates.save_email_template(s, name="HDFC UPI debit", spec=seed("hdfc_upi_debit"), status="active").id
        credit: str = templates.save_email_template(s, name="HDFC UPI credit", spec=seed("hdfc_upi_credit"), status="active").id
    assert route(db, email(DEBIT)).template_id == debit
    assert route(db, email(CREDIT)).template_id == credit


@pytest.mark.parametrize("debit_first", [True, False])
def test_same_sender_and_same_subject_are_told_apart_by_body(debit_first: bool) -> None:
    # In the fixtures the debit and credit subjects differ, so force them to one shared
    # subject pattern: now only the critical-field gate can route the emails.
    specs: dict[str, dict[str, Any]] = {}
    for name in ("hdfc_upi_debit", "hdfc_upi_credit"):
        specs[name] = copy.deepcopy(seed(name))
        specs[name]["match"]["subject"] = "."
    order: list[str] = ["hdfc_upi_debit", "hdfc_upi_credit"] if debit_first else ["hdfc_upi_credit", "hdfc_upi_debit"]
    candidates: list[EmailTemplate] = [tmpl(specs[n], n) for n in order]
    for fixture, winner in ((DEBIT, "hdfc_upi_debit"), (CREDIT, "hdfc_upi_credit")):
        result: Interpretation = interpret(candidates, email(fixture))
        assert result.template_id == winner
        assert result.extraction == expected(fixture)
        if result.attempts[0].template_id != winner:  # the other one was tried first and declined on a critical field
            assert not result.attempts[0].matched
            assert "amount" in [f.field for f in result.attempts[0].failures]


def broken_amount(spec: dict[str, Any]) -> dict[str, Any]:
    spec = copy.deepcopy(spec)
    spec["fields"]["amount"]["pattern"] = spec["fields"]["amount"]["pattern"].replace("debited", "withdrawn")
    assert spec["fields"]["amount"] != seed("hdfc_upi_debit")["fields"]["amount"]
    return spec


def test_a_broken_amount_pattern_means_not_matching_and_the_next_candidate_wins() -> None:
    result: Interpretation = interpret([tmpl(broken_amount(seed("hdfc_upi_debit")), "broken"), tmpl(seed("hdfc_upi_debit"), "good")],
                       email(DEBIT))
    assert [(a.template_id, a.matched) for a in result.attempts] == [("broken", False), ("good", True)]
    assert [f.field for f in result.attempts[0].failures] == ["amount"]
    assert result.template_id == "good" and result.extraction == expected(DEBIT)


def test_a_broken_amount_pattern_alone_emits_no_transaction() -> None:
    result: Interpretation = interpret([tmpl(broken_amount(seed("hdfc_upi_debit")), "broken")], email(DEBIT))
    assert result.extraction is None  # falls through to tier 2; never a transaction with a null amount
    assert result.template_id is None


def test_the_mandate_notice_shares_sender_and_subject_with_the_credit_alert_but_is_declined() -> None:
    mandate, credit = email(MANDATE), email(CREDIT)
    assert (mandate.sender, mandate.subject) == (credit.sender, credit.subject)
    result: Interpretation = interpret([tmpl(seed("hdfc_upi_credit"), "credit")], mandate)
    assert result.extraction is None
    assert not result.attempts[0].matched
