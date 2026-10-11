"""Statement mode of the authoring harness (build-plan slices 9-10).

    python -m hisaab try-template SPEC.json STATEMENT.pdf [--text] [--password-env VAR]

Prints detection, account binding, every parsed row, every unparseable row
with its raw cells and reason, then the arithmetic validation: declared vs
computed closing and the gap. --text prints every line of every page, prefixed
with its page number: what detect, ends_at, stop_at and line patterns run against.

A password is read from the environment variable named by --password-env,
never from the command line, so it stays out of shell history.

Like the email side, nothing here parses anything itself: rows and validation
come from statement_interpreter, the code production runs.
"""

import os
from pathlib import Path

from hisaab.parsing.pdf import PasswordRequired, PdfError, read_pdf
from hisaab.parsing.statement_interpreter import TemplateMismatch, detect, interpret_statement
from hisaab.parsing.statement_spec import check_statement_spec


def rupees(paise: int) -> str:
    """₹1,23,456.78 (Indian grouping), for display only."""
    sign, paise = ("-" if paise < 0 else ""), abs(paise)
    whole, frac = divmod(paise, 100)
    digits = str(whole)
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    grouped = ",".join(([head] if head else []) + groups + [tail])
    return f"{sign}₹{grouped}.{frac:02d}"


def try_statement(raw_spec: dict, paths: list[str], show_text: bool, password_env: str | None, say) -> int:
    spec, problems = check_statement_spec(raw_spec)
    if problems:
        say("Spec problems; this template cannot be saved or run until they are fixed:")
        for p in problems:
            say(f"  - {p}")
        return 1
    password = os.environ.get(password_env) if password_env else None
    worst = 0
    for path in paths:
        try:
            pages = read_pdf(Path(path).read_bytes(), password)
        except OSError as e:
            say(f"Can't read {path}: {e.strerror}")
            return 2
        except PasswordRequired as e:
            say(f"{path}: {e} Put it in an environment variable and pass --password-env NAME.")
            return 2
        except PdfError as e:
            say(f"{path}: {e}")
            return 2
        say(f"\n=== {path}  ({len(pages)} pages)")
        worst = max(worst, _report(spec, pages, show_text, say))
    return worst


def _report(spec, pages, show_text, say) -> int:
    if show_text:
        for page in pages:
            for line in page.lines:
                say(f"  p{page.number} | {line.text}")
        say()
    ok = detect(spec, pages)
    if ok:
        say(f"  detect   ok (page {spec.detect_page})")
    else:
        text = pages[spec.detect_page - 1].text if len(pages) >= spec.detect_page else ""
        missing = [p for p in spec.all_of if p not in text]
        present = [p for p in spec.none_of if p in text]
        say(f"  detect   FAILED on page {spec.detect_page}: missing {missing}, excluded phrases present {present}")
    try:
        result = interpret_statement(spec, pages)
    except TemplateMismatch as e:
        say(f"  DOES NOT FIT: {e}. Production would fall through to tier 3.")
        return 1
    say(f"  account  {' '.join(result.account) if result.account else 'not found (the preview would ask)'}")
    say(f"\n  {len(result.rows)} rows, {result.rows_skipped} skipped, {len(result.unparseable)} unparseable")
    for r in result.rows:
        when = r.date + (f" {r.time[:5]}" if r.time else "")
        balance = f"  bal {rupees(r.balance)}" if r.balance is not None else ""
        say(f"  p{r.page} {when:16} {r.direction:6} {rupees(r.amount):>14}  {r.narration[:60]}{balance}")
    for u in result.unparseable:
        say(f"  p{u['page']} unparseable: {u['reason']}  cells={u['raw_cells']}")

    v = result.validation
    declared, computed = v["declared"], v["computed"]
    say(f"\n  credits {rupees(computed['credits'])}   debits {rupees(computed['debits'])}")
    if v["outcome"] == "unavailable":
        missing = [k for k in ("opening", "closing") if declared[k] is None]
        say(f"  UNAVAILABLE: declared {' and '.join(missing)} not found, so the rows can't be cross-checked.")
        return 1
    sense = "opening + debits - credits" if spec.account_kind == "credit_card" else "opening + credits - debits"
    say(f"  declared opening {rupees(declared['opening'])}   declared closing {rupees(declared['closing'])}")
    say(f"  computed closing {rupees(computed['closing'])}   ({sense})")
    say(f"  {v['outcome'].upper()}: gap {rupees(v['gap_paise'])} (declared - computed; tolerance ₹1)")
    return 0 if ok and v["outcome"] == "passed" and not result.unparseable else 1
