"""`python -m hisaab create-admin`: bootstrap the first admin (there is no self-signup)."""

import io
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine

import hisaab.__main__ as entry
from hisaab import cli
from hisaab.auth import users
from hisaab.config import Settings
from hisaab.db import Database, make_engine
from tests.conftest import login


def run(
    settings: Settings, argv: list[str], stdin_text: str = "", monkeypatch: pytest.MonkeyPatch | None = None,
) -> int:
    if monkeypatch is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    return cli.main(argv, settings=settings)


def test_create_admin_from_stdin_then_log_in(settings: Settings, migrated_engine: Engine, client: TestClient,
                                             monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    code: int = run(settings, ["create-admin", "--username", "Asha", "--display-name", "Asha K", "--password-stdin"],
               "first admin pw\n", monkeypatch)
    assert code == 0
    assert "Created admin 'asha'" in capsys.readouterr().out
    resp: Response = login(client, "asha", "first admin pw")
    assert resp.status_code == 200 and resp.json()["role"] == "admin"


def test_password_prompt_requires_matching_confirmation(settings: Settings, migrated_engine: Engine,
                                                        monkeypatch: pytest.MonkeyPatch,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    answers: Iterator[str] = iter(["one password", "another password"])
    monkeypatch.setattr(cli, "getpass", lambda prompt="": next(answers))
    code: int = run(settings, ["create-admin", "--username", "asha", "--display-name", "Asha"])
    assert code == 1
    assert "do not match" in capsys.readouterr().err
    with Database(migrated_engine).read_session() as s:
        assert users.list_users(s) == []


def test_password_prompt_happy_path(
    settings: Settings, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "getpass", lambda prompt="": "same password 1")
    assert run(settings, ["create-admin", "--username", "asha", "--display-name", "Asha"]) == 0


def test_duplicate_username_exits_1_with_message(settings: Settings, migrated_engine: Engine,
                                                 monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    args: list[str] = ["create-admin", "--username", "asha", "--display-name", "Asha", "--password-stdin"]
    assert run(settings, args, "first admin pw\n", monkeypatch) == 0
    assert run(settings, args, "first admin pw\n", monkeypatch) == 1
    assert "already taken" in capsys.readouterr().err


def test_rule_violation_exits_1_with_message(settings: Settings, migrated_engine: Engine,
                                             monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    code: int = run(settings, ["create-admin", "--username", "asha", "--display-name", "Asha", "--password-stdin"],
               "short\n", monkeypatch)
    assert code == 1
    assert "at least 8" in capsys.readouterr().err


def test_unmigrated_database_says_to_migrate(settings: Settings, monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    make_engine(settings.database_path).dispose()  # file exists, no schema
    code: int = run(settings, ["create-admin", "--username", "asha", "--display-name", "Asha", "--password-stdin"],
               "first admin pw\n", monkeypatch)
    assert code == 1
    assert "alembic upgrade head" in capsys.readouterr().err


def test_no_arguments_starts_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(entry, "serve", lambda: called.append("serve"))
    entry.main([])
    assert called == ["serve"]


def test_subcommand_dispatches_to_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "main", lambda argv: 7)  # the module __main__ imported
    with pytest.raises(SystemExit) as exit_info:
        entry.main(["create-admin"])
    assert exit_info.value.code == 7


def test_sync_seeds_loads_the_shipped_templates_and_reports_counts(
    settings: Settings, migrated_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["sync-seeds"], settings=settings) == 0
    first: str = capsys.readouterr().out
    assert "templates: " in first and "added" in first
    assert cli.main(["sync-seeds"], settings=settings) == 0
    assert "templates: 0 added, 0 updated" in capsys.readouterr().out  # idempotent


def test_measure_fixtures_on_an_empty_corpus_reports_nothing_measured_and_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["measure-fixtures", "--fixtures", str(tmp_path)]) == 1
    assert "0 transactions" in capsys.readouterr().out
