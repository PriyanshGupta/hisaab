"""The OpenAPI schema the frontend's generated client is built from."""

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from hisaab import cli
from hisaab.config import Settings
from hisaab.main import create_app
from hisaab.openapi_export import build_openapi_schema


def test_schema_has_the_auth_and_admin_routes() -> None:
    paths: Any = build_openapi_schema()["paths"]
    assert {"/api/auth/login", "/api/auth/logout", "/api/auth/me", "/api/admin/users"} <= set(paths)


def test_me_response_model_exposes_display_name() -> None:
    schema: dict[str, Any] = build_openapi_schema()
    me: Any = schema["paths"]["/api/auth/me"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    model: Any = schema["components"]["schemas"][me["$ref"].rsplit("/", 1)[1]]
    assert "display_name" in model["properties"]


def test_building_the_schema_needs_no_database_or_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HISAAB_SESSION_SECRET", raising=False)
    build_openapi_schema()
    assert list(tmp_path.iterdir()) == []  # nothing created (no .db file)


def test_export_writes_stable_sorted_json(tmp_path: Path) -> None:
    out: Path = tmp_path / "openapi.json"
    assert cli.main(["export-openapi", str(out)]) == 0
    first: str = out.read_text()
    assert cli.main(["export-openapi", str(out)]) == 0
    assert out.read_text() == first
    assert json.loads(first)["info"]["title"] == "Hisaab"
    assert first == json.dumps(json.loads(first), indent=2, sort_keys=True) + "\n"


def test_exported_paths_match_the_running_app(settings: Settings, migrated_engine: Engine) -> None:
    assert build_openapi_schema()["paths"] == create_app(settings).openapi()["paths"]
