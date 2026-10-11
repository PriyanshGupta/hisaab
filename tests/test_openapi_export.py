"""The OpenAPI schema the frontend's generated client is built from."""

import json

from hisaab import cli
from hisaab.main import create_app
from hisaab.openapi_export import build_openapi_schema


def test_schema_has_the_auth_and_admin_routes():
    paths = build_openapi_schema()["paths"]
    assert {"/api/auth/login", "/api/auth/logout", "/api/auth/me", "/api/admin/users"} <= set(paths)


def test_me_response_model_exposes_display_name():
    schema = build_openapi_schema()
    me = schema["paths"]["/api/auth/me"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    model = schema["components"]["schemas"][me["$ref"].rsplit("/", 1)[1]]
    assert "display_name" in model["properties"]


def test_building_the_schema_needs_no_database_or_secret(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HISAAB_SESSION_SECRET", raising=False)
    build_openapi_schema()
    assert list(tmp_path.iterdir()) == []  # nothing created (no .db file)


def test_export_writes_stable_sorted_json(tmp_path):
    out = tmp_path / "openapi.json"
    assert cli.main(["export-openapi", str(out)]) == 0
    first = out.read_text()
    assert cli.main(["export-openapi", str(out)]) == 0
    assert out.read_text() == first
    assert json.loads(first)["info"]["title"] == "Hisaab"
    assert first == json.dumps(json.loads(first), indent=2, sort_keys=True) + "\n"


def test_exported_paths_match_the_running_app(settings, migrated_engine):
    assert build_openapi_schema()["paths"] == create_app(settings).openapi()["paths"]
