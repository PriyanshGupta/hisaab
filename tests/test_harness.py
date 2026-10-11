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


def test_a_statement_spec_points_at_slice_10(tmp_path, capsys):
    code, out = run(capsys, spec_file(tmp_path, {"version": 1, "artifact_type": "statement"}), eml(tmp_path))
    assert code == 2 and "statement" in out.lower()


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
