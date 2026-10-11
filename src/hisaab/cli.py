"""Command-line administration: `python -m hisaab <command>`.

try-template runs a template spec over fixture emails: the authoring loop
(parsing/harness.py). sync-seeds loads the shipped templates and normalization
patterns (seeds.py); scripts/run.sh runs it after migrating. measure-fixtures
prints the PRD §9 parse rate over the local fixture corpus (parsing/measure.py).

create-admin bootstraps the first admin, since there is no self-signup
(PRD §6.9). It is also the recovery path if every admin is locked out.
The password is read by a hidden prompt (or from stdin with --password-stdin,
for scripting) and never accepted as an argument, so it cannot end up in
shell history or the process list.
"""

import argparse
import json
import sys
from getpass import getpass
from pathlib import Path

from sqlalchemy import inspect

from hisaab.auth import users
from hisaab.config import PROJECT_ROOT, Settings
from hisaab.db import Database
from hisaab.models import User


def _read_password(from_stdin: bool) -> str | None:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first: str = getpass("Password: ")
    if getpass("Confirm password: ") != first:
        print("Passwords do not match.", file=sys.stderr)
        return None
    return first


def _create_admin(args: argparse.Namespace, settings: Settings) -> int:
    db: Database = Database(settings.database_path)
    try:
        if not inspect(db.write_engine).has_table("users"):  # the write engine creates a missing file
            print(f"Database at {settings.database_path} has no schema. Run: .venv/bin/alembic upgrade head",
                  file=sys.stderr)
            return 1
        password: str | None = _read_password(args.password_stdin)
        if password is None:
            return 1
        try:
            with db.write_session() as session:
                user: User = users.create_user(session, args.username, args.display_name, password, role="admin")
                username: str = user.username
        except users.UserError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    finally:
        db.dispose()
    print(f"Created admin '{username}'.")
    return 0


def _export_openapi(args: argparse.Namespace) -> int:
    from hisaab.openapi_export import build_openapi_schema

    # Sorted and indented so the file is stable across runs and diffs cleanly.
    Path(args.output).write_text(json.dumps(build_openapi_schema(), indent=2, sort_keys=True) + "\n")
    return 0


def _sync_seeds(settings: Settings) -> int:
    from hisaab.seeds import SeedReport, sync_seeds

    db: Database = Database(settings.database_path)
    try:
        with db.write_session() as session:
            report: SeedReport = sync_seeds(session)
    finally:
        db.dispose()
    print(f"templates: {report.templates_added} added, {report.templates_updated} updated; "
          f"payees: {report.payees_added} added; patterns: {report.patterns_added} added, "
          f"{report.patterns_updated} updated; senders: {report.senders_added} added")
    return 0


HELD_OUT: frozenset[str] = frozenset({"hdfc_cc_purchase_02", "icici_cc_purchase_02", "scapia_cc_purchase_02"})


def _measure_fixtures(args: argparse.Namespace) -> int:
    from hisaab.parsing.measure import FixtureResult, Summary, load_seed_templates, measure, summarize
    from hisaab.seeds import SEED_TEMPLATES

    results: list[FixtureResult] = measure(Path(args.fixtures), load_seed_templates(SEED_TEMPLATES), HELD_OUT)
    for r in results:
        mark: str = "ok  " if r.ok else "FAIL"
        held: str = " (held out)" if r.held_out else ""
        count: str = "notice" if r.kind == "notice" else f"{r.correct}/{r.transactions}"
        print(f"{mark} {r.name:28} {count:>7}  via {r.template or '-'}{held}")
        if r.detail:
            print(f"       {r.detail}")
    summary: Summary = summarize(results)
    print(f"\n{summary.correct} of {summary.transactions} transactions with correct amount, date and account: "
          f"{summary.rate:.1%} (target 95%)")
    if summary.held_out_transactions:
        print(f"held out: {summary.held_out_correct} of {summary.held_out_transactions}")
    return 0 if summary.transactions and summary.rate >= 0.95 else 1


def main(argv: list[str], settings: Settings | None = None) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="python -m hisaab", description="Hisaab administration.")
    commands: argparse._SubParsersAction[argparse.ArgumentParser] = parser.add_subparsers(dest="command", required=True)
    create: argparse.ArgumentParser = commands.add_parser("create-admin", help="create an admin user")
    create.add_argument("--username", required=True)
    create.add_argument("--display-name", required=True)
    create.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    export: argparse.ArgumentParser = commands.add_parser(
        "export-openapi", help="write the OpenAPI schema (for the frontend client)")
    export.add_argument("output", help="path of the JSON file to write")
    tryout: argparse.ArgumentParser = commands.add_parser(
        "try-template", help="run a template spec over fixture emails, field by field")
    tryout.add_argument("spec", help="template spec JSON file")
    tryout.add_argument("fixtures", nargs="+", help=".eml files (or statement PDFs) to run it on")
    tryout.add_argument("--text", action="store_true", help="also print the text the patterns run against")
    tryout.add_argument("--password-env", metavar="VAR", help="statement PDFs: read the password from this env var")
    skel: argparse.ArgumentParser = commands.add_parser(
        "template-skeleton", help="print a starting template spec for a fixture email")
    skel.add_argument("fixture", help=".eml file")
    meas: argparse.ArgumentParser = commands.add_parser(
        "measure-fixtures", help="PRD §9 parse rate: every fixture with an expected answer, through the seeds")
    meas.add_argument("--fixtures", default=str(PROJECT_ROOT / "fixtures"), help="fixture directory")
    commands.add_parser("sync-seeds", help="load the shipped parser templates and normalization patterns")
    args: argparse.Namespace = parser.parse_args(argv)
    if args.command == "create-admin":
        return _create_admin(args, settings or Settings())
    if args.command == "export-openapi":
        return _export_openapi(args)
    if args.command == "try-template":
        from hisaab.parsing.harness import try_template

        return try_template(args.spec, args.fixtures, args.text, sys.stdout, args.password_env)
    if args.command == "measure-fixtures":
        return _measure_fixtures(args)
    if args.command == "sync-seeds":
        return _sync_seeds(settings or Settings())
    if args.command == "template-skeleton":
        from hisaab.parsing.harness import skeleton

        print(json.dumps(skeleton(args.fixture), indent=2, ensure_ascii=False))
        return 0
    return 2  # unreachable: argparse rejects unknown commands
