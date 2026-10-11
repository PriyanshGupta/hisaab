"""Serving the built SPA from FastAPI: real files, index.html for client routes, never for /api."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine

from hisaab.config import Settings
from hisaab.main import create_app

INDEX: str = "<!doctype html><title>Hisaab</title><div id=root></div>"
APP_JS: str = "console.log('hisaab');"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    d: Path = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text(INDEX)
    (d / "assets" / "app-abc123.js").write_text(APP_JS)
    return d


@pytest.fixture
def spa_client(settings: Settings, migrated_engine: Engine, dist: Path) -> Iterator[TestClient]:
    settings = settings.model_copy(update={"frontend_dist": dist})
    with TestClient(create_app(settings)) as c:
        yield c


def test_root_serves_index_without_caching(spa_client: TestClient) -> None:
    resp: Response = spa_client.get("/")
    assert resp.status_code == 200 and resp.text == INDEX
    assert resp.headers["cache-control"] == "no-cache"


def test_client_route_refresh_serves_index(spa_client: TestClient) -> None:
    resp: Response = spa_client.get("/reports/2026-08")
    assert resp.status_code == 200 and resp.text == INDEX


def test_built_assets_are_served(spa_client: TestClient) -> None:
    resp: Response = spa_client.get("/assets/app-abc123.js")
    assert resp.status_code == 200 and resp.text == APP_JS


def test_unknown_api_path_is_json_404_not_index(spa_client: TestClient) -> None:
    resp: Response = spa_client.get("/api/does-not-exist")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")


def test_api_routes_still_win(spa_client: TestClient) -> None:
    resp: Response = spa_client.get("/api/auth/me")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not logged in"}


def test_health_still_json(spa_client: TestClient) -> None:
    assert spa_client.get("/health").json() == {"status": "ok", "database": "ok"}


def test_missing_file_with_extension_is_404_not_index(spa_client: TestClient) -> None:
    assert spa_client.get("/favicon.ico").status_code == 404


@pytest.mark.parametrize("path", ["/%2e%2e/%2e%2e/%2e%2e/etc/passwd", "/../../../../etc/passwd", "/assets/../../../etc/passwd"])
def test_path_traversal_never_leaks_files(spa_client: TestClient, path: str) -> None:
    resp: Response = spa_client.get(path)
    assert "root:" not in resp.text


def test_without_a_build_root_explains_itself(settings: Settings, migrated_engine: Engine, tmp_path: Path) -> None:
    settings = settings.model_copy(update={"frontend_dist": tmp_path / "no-dist"})
    with TestClient(create_app(settings)) as c:
        resp: Response = c.get("/")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Frontend not built. Run scripts/run.sh (or npm run build in frontend/)."}
