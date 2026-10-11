"""`python -m hisaab try-template`: the spec authoring loop (build-plan slice 9). Synthetic emails only."""

import copy
import json
from email.message import EmailMessage

import pytest

from hisaab import cli
from tests.test_email_interpreter import BODY
from tests.test_email_spec import SPEC


def eml(tmp_path, name="alert.eml", body=BODY, sender="Alerts@Example-Bank.in", subject="Debit alert"):
    msg = EmailMessage()
    msg["From"], msg["Subject"], msg["Date"] = sender, subject, "Mon, 05 Oct 2026 20:00:00 +0000"
    msg.set_content(body, subtype="html")
    path = tmp_path / name
    path.write_bytes(msg.as_bytes())
    return str(path)


def spec_file(tmp_path, spec=SPEC, name="spec.json"):
    path = tmp_path / name
    path.write_text(spec if isinstance(spec, str) else json.dumps(spec, indent=2))
    return str(path)


def run(capsys, *argv):
    code = cli.main(["try-template", *argv])
    return code, capsys.readouterr().out


def test_a_matching_spec_shows_each_field_capture_and_value(tmp_path, capsys):
    code, out = run(capsys, spec_file(tmp_path), eml(tmp_path))
    assert code == 0
    assert "MATCHED" in out and "NOT MATCHED" not in out
    assert '"1,234.50" -> 123450' in out
    assert "const -> 'debit'" in out
    assert "not in this format" in out  # time / balance
    assert "shop.owner@okbank" in out


def test_a_failing_critical_field_is_named_with_its_reason(tmp_path, capsys):
    broken = copy.deepcopy(SPEC)
    broken["fields"]["amount"]["pattern"] = r"INR ([\d,.]+) spent"
    code, out = run(capsys, spec_file(tmp_path, broken), eml(tmp_path))
    assert code == 1
    assert "NOT MATCHED" in out
    amount_line = next(line for line in out.splitlines() if line.strip().startswith("amount"))
    assert "critical" in amount_line and "no match" in amount_line


def test_a_transform_failure_shows_what_was_captured(tmp_path, capsys):
    code, out = run(capsys, spec_file(tmp_path), eml(tmp_path, body=BODY.replace("05-10-26", "31-02-26")))
    assert code == 1
    assert '"31-02-26"' in out and "not a date in the form %d-%m-%y" in out


def test_a_required_non_critical_failure_means_review(tmp_path, capsys):
    code, out = run(capsys, spec_file(tmp_path), eml(tmp_path, body=BODY.replace("Ref 9988776655", "")))
    assert code == 1
    assert "needs review" in out and "reference_number" in out


def test_text_shows_what_the_patterns_run_against(tmp_path, capsys):
    code, out = run(capsys, spec_file(tmp_path), eml(tmp_path), "--text")
    assert "Rs. 1,234.50 debited from account ending 1111" in out
    assert "<table>" not in out


def test_sender_and_subject_mismatches_are_reported_and_fields_still_run(tmp_path, capsys):
    code, out = run(capsys, spec_file(tmp_path), eml(tmp_path, sender="other@bank.in", subject="Hello"))
    assert code == 1
    assert "sender" in out and "other@bank.in" in out and "would never try this template" in out
    assert '"1,234.50" -> 123450' in out
    assert "NOT TRIED" in out and "MATCHED: every field" not in out  # every field extracts, but it'd never run


def test_an_invalid_spec_lists_every_problem_and_still_runs_the_valid_fields(tmp_path, capsys):
    half = copy.deepcopy(SPEC)
    half["fields"]["payee_raw"] = {"pattern": "no group"}
    half["fields"]["direction"] = {"const": "out"}
    code, out = run(capsys, spec_file(tmp_path, half), eml(tmp_path))
    assert code == 1
    assert "payee_raw: pattern needs a capture group" in out and "direction: const must be debit or credit" in out
    assert '"1,234.50" -> 123450' in out
    assert "cannot be saved" in out


def test_a_json_syntax_error_gives_line_and_column(tmp_path, capsys):
    code, out = run(capsys, spec_file(tmp_path, '{\n  "version": 1,\n  "match": {,}\n}'), eml(tmp_path))
    assert code == 2
    assert "line 3" in out and "column" in out


def test_a_missing_fixture_is_a_plain_error(tmp_path, capsys):
    code, out = run(capsys, spec_file(tmp_path), str(tmp_path / "nope.eml"))
    assert code == 2 and "nope.eml" in out



def test_several_fixtures_get_a_block_each_and_one_failure_fails_the_run(tmp_path, capsys):
    good = eml(tmp_path, "good.eml")
    bad = eml(tmp_path, "bad.eml", body=BODY.replace("account ending 1111", "your account"))
    code, out = run(capsys, spec_file(tmp_path), good, bad)
    assert code == 1
    assert "good.eml" in out and "bad.eml" in out
    assert out.count("NOT MATCHED") == 1


# --- starting a spec: the skeleton (pain point 1 while authoring ICICI) ----------------

def test_the_skeleton_is_a_runnable_starting_point_built_from_the_email(tmp_path, capsys):
    code = cli.main(["template-skeleton", eml(tmp_path, subject="Debit alert (a.k.a. spend) for A/c")])
    skeleton = capsys.readouterr().out
    assert code == 0
    spec = json.loads(skeleton)
    assert spec["match"]["sender"] == "alerts@example-bank.in"
    assert spec["preprocess"] == ["strip_html", "collapse_whitespace"]
    from hisaab.parsing.email_spec import CONTRACT_FIELDS, check_email_spec

    assert list(spec["fields"]) == list(CONTRACT_FIELDS)
    parsed, problems = check_email_spec(spec)
    # the subject pattern is the escaped subject, so it matches it and nothing broader
    assert parsed.subject.search("Debit alert (a.k.a. spend) for A/c")
    assert not parsed.subject.search("Debit alert (aXkXaX spend) for A/c")
    assert "Debit alert" in spec["match"]["subject"]  # spaces left readable, not "Debit\\ alert"
    # what is left to do is exactly the empty patterns
    assert sorted(problems) == sorted(f"{f}: pattern is empty" for f in
                                      ("amount", "account_identifier", "payee_raw", "date"))


def test_a_fields_spec_problem_is_shown_on_its_own_line(tmp_path, capsys):
    half = copy.deepcopy(SPEC)
    half["fields"]["payee_raw"] = {"pattern": ""}
    _, out = run(capsys, spec_file(tmp_path, half), eml(tmp_path))
    payee_line = next(line for line in out.splitlines() if line.strip().startswith("payee_raw"))
    assert "pattern is empty" in payee_line


# --- statements (slice 10) ----------------------------------------------------------------

from tests.statement_samples import ruled_page  # noqa: E402
from tests.pdf_factory import make_pdf  # noqa: E402
from tests.test_statement_interpreter import ROWS  # noqa: E402
from tests.test_statement_spec import RULED  # noqa: E402

SUMMARY_OK = ["STATEMENT SUMMARY", "Opening 1,000.00", "Closing 1,450.00"]


def statement(tmp_path, page, name="stmt.pdf", password=None):
    path = tmp_path / name
    path.write_bytes(make_pdf([page], password))
    return str(path)


def test_a_statement_spec_prints_rows_and_a_passing_validation(tmp_path, capsys):
    code, out = run(capsys, spec_file(tmp_path, RULED), statement(tmp_path, ruled_page(ROWS, first=True, summary=SUMMARY_OK)))
    assert code == 0
    assert "detect   ok" in out and "account  last4 1234" in out
    assert "2026-07-02" in out and "credit" in out and "500.00" in out and "SALARY JULY" in out
    assert "3 rows" in out
    assert "PASSED" in out


def test_a_failing_validation_shows_declared_computed_and_the_gap(tmp_path, capsys):
    page = ruled_page(ROWS, first=True, summary=["STATEMENT SUMMARY", "Opening 1,000.00", "Closing 1,550.00"])
    code, out = run(capsys, spec_file(tmp_path, RULED), statement(tmp_path, page))
    assert code == 1
    assert "FAILED" in out and "gap ₹100.00" in out and "declared closing ₹1,550.00" in out
    assert "computed closing ₹1,450.00" in out


def test_unavailable_says_why_and_is_not_a_pass(tmp_path, capsys):
    code, out = run(capsys, spec_file(tmp_path, RULED), statement(tmp_path, ruled_page(ROWS, first=True)))
    assert code == 1
    assert "UNAVAILABLE" in out and "closing not found" in out


def test_unparseable_rows_are_shown_with_their_cells_and_reason(tmp_path, capsys):
    page = ruled_page([*ROWS, ("04/07/26", "ODD", "9", "5.00", "5.00", "")], first=True, summary=SUMMARY_OK)
    code, out = run(capsys, spec_file(tmp_path, RULED), statement(tmp_path, page))
    assert code == 1
    assert "unparseable" in out and "both debit and credit are filled" in out and "ODD" in out


def test_a_template_mismatch_is_explained(tmp_path, capsys):
    page = ruled_page(ROWS, first=True, labels=["Date", "Narration", "Ref", "Deposit", "Withdrawal", "Balance"])
    code, out = run(capsys, spec_file(tmp_path, RULED), statement(tmp_path, page))
    assert code == 1 and "DOES NOT FIT" in out and "Withdrawal" in out


def test_detection_failure_is_reported(tmp_path, capsys):
    spec = copy.deepcopy(RULED)
    spec["detect"]["all_of"] = ["Some Other Bank"]
    code, out = run(capsys, spec_file(tmp_path, spec), statement(tmp_path, ruled_page(ROWS, first=True, summary=SUMMARY_OK)))
    assert code == 1 and "detect   FAILED" in out and "Some Other Bank" in out


def test_text_prints_each_page_line_with_its_page(tmp_path, capsys):
    code, out = run(capsys, spec_file(tmp_path, RULED), statement(tmp_path, ruled_page(ROWS, first=True, summary=SUMMARY_OK)),
                    "--text")
    assert "p1 | Date Narration Ref Withdrawal Deposit Balance" in out


def test_a_protected_statement_reads_its_password_from_the_named_environment_variable(tmp_path, capsys, monkeypatch):
    path = statement(tmp_path, ruled_page(ROWS, first=True, summary=SUMMARY_OK), password="s3cret")
    code, out = run(capsys, spec_file(tmp_path, RULED), path)
    assert code == 2 and "password-protected" in out and "--password-env" in out
    monkeypatch.setenv("STMT_PW", "s3cret")
    code, out = run(capsys, spec_file(tmp_path, RULED), path, "--password-env", "STMT_PW")
    assert code == 0 and "PASSED" in out
    monkeypatch.setenv("STMT_PW", "nope")
    code, out = run(capsys, spec_file(tmp_path, RULED), path, "--password-env", "STMT_PW")
    assert code == 2 and "doesn't open" in out


def test_an_invalid_statement_spec_lists_its_problems(tmp_path, capsys):
    spec = copy.deepcopy(RULED)
    spec["account_kind"] = "wallet"
    code, out = run(capsys, spec_file(tmp_path, spec), statement(tmp_path, ruled_page(ROWS, first=True)))
    assert code == 1 and "account_kind" in out
