"""Command-line administration: `python -m hisaab <command>`.

create-admin bootstraps the first admin, since there is no self-signup
(PRD §6.9). It is also the recovery path if every admin is locked out.
The password is read by a hidden prompt (or from stdin with --password-stdin,
for scripting) and never accepted as an argument, so it cannot end up in
shell history or the process list.
"""

import argparse
import sys
from getpass import getpass

from sqlalchemy import inspect

from hisaab.auth import users
from hisaab.config import Settings
from hisaab.db import Database, make_engine


def _read_password(from_stdin: bool) -> str | None:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass("Password: ")
    if getpass("Confirm password: ") != first:
        print("Passwords do not match.", file=sys.stderr)
        return None
    return first


def _create_admin(args: argparse.Namespace, settings: Settings) -> int:
    engine = make_engine(settings.database_path)
    try:
        if not inspect(engine).has_table("users"):
            print(f"Database at {settings.database_path} has no schema. Run: .venv/bin/alembic upgrade head",
                  file=sys.stderr)
            return 1
        password = _read_password(args.password_stdin)
        if password is None:
            return 1
        try:
            with Database(engine).write_session() as session:
                user = users.create_user(session, args.username, args.display_name, password, role="admin")
                username = user.username
        except users.UserError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    finally:
        engine.dispose()
    print(f"Created admin '{username}'.")
    return 0


def main(argv: list[str], settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hisaab", description="Hisaab administration.")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-admin", help="create an admin user")
    create.add_argument("--username", required=True)
    create.add_argument("--display-name", required=True)
    create.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    args = parser.parse_args(argv)
    settings = settings or Settings()
    if args.command == "create-admin":
        return _create_admin(args, settings)
    return 2  # unreachable: argparse rejects unknown commands
