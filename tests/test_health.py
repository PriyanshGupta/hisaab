from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from hisaab.main import create_app


def test_health_reports_ok_with_working_database(settings):
    with TestClient(create_app(settings)) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}
    assert settings.database_path.exists()


def test_health_returns_503_when_database_unreachable(settings):
    app = create_app(settings)
    app.state.db.engine = create_engine("sqlite:////nonexistent-hisaab-dir/x.db")
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json() == {"status": "error", "database": "unreachable"}
