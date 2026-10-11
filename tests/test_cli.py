"""`python -m hisaab create-admin`: bootstrap the first admin (there is no self-signup)."""

import io

import pytest

import hisaab.__main__ as entry
from hisaab import cli
from hisaab.auth import users
from hisaab.db import Database, make_engine
from tests.conftest import login


def run(settings, argv, stdin_text="", monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    return cli.main(argv, settings=settings)


def test_create_admin_from_stdin_then_log_in(settings, migrated_engine, client, monkeypatch, capsys):
    code = run(settings, ["create-admin", "--username", "Asha", "--display-name", "Asha K", "--password-stdin"],
               "first admin pw\n", monkeypatch)
    assert code == 0
    assert "Created admin 'asha'" in capsys.readouterr().out
    resp = login(client, "asha", "first admin pw")
    assert resp.status_code == 200 and resp.json()["role"] == "admin"


def test_password_prompt_requires_matching_confirmation(settings, migrated_engine, monkeypatch, capsys):
    answers = iter(["one password", "another password"])
    monkeypatch.setattr(cli, "getpass", lambda prompt="": next(answers))
    code = run(settings, ["create-admin", "--username", "asha", "--display-name", "Asha"])
    assert code == 1
    assert "do not match" in capsys.readouterr().err
    with Database(migrated_engine).read_session() as s:
        assert users.list_users(s) == []


def test_password_prompt_happy_path(settings, migrated_engine, monkeypatch):
    monkeypatch.setattr(cli, "getpass", lambda prompt="": "same password 1")
    assert run(settings, ["create-admin", "--username", "asha", "--display-name", "Asha"]) == 0


def test_duplicate_username_exits_1_with_message(settings, migrated_engine, monkeypatch, capsys):
    args = ["create-admin", "--username", "asha", "--display-name", "Asha", "--password-stdin"]
    assert run(settings, args, "first admin pw\n", monkeypatch) == 0
    assert run(settings, args, "first admin pw\n", monkeypatch) == 1
    assert "already taken" in capsys.readouterr().err


def test_rule_violation_exits_1_with_message(settings, migrated_engine, monkeypatch, capsys):
    code = run(settings, ["create-admin", "--username", "asha", "--display-name", "Asha", "--password-stdin"],
               "short\n", monkeypatch)
    assert code == 1
    assert "at least 8" in capsys.readouterr().err


def test_unmigrated_database_says_to_migrate(settings, monkeypatch, capsys):
    make_engine(settings.database_path).dispose()  # file exists, no schema
    code = run(settings, ["create-admin", "--username", "asha", "--display-name", "Asha", "--password-stdin"],
               "first admin pw\n", monkeypatch)
    assert code == 1
    assert "alembic upgrade head" in capsys.readouterr().err


def test_no_arguments_starts_the_server(monkeypatch):
    called = []
    monkeypatch.setattr(entry, "serve", lambda: called.append("serve"))
    entry.main([])
    assert called == ["serve"]


def test_subcommand_dispatches_to_cli(monkeypatch):
    monkeypatch.setattr(entry.cli, "main", lambda argv: 7)
    with pytest.raises(SystemExit) as exit_info:
        entry.main(["create-admin"])
    assert exit_info.value.code == 7
