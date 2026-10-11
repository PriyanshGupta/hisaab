from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import create_engine


def test_health_reports_ok_with_working_database(client: TestClient) -> None:
    resp: Response = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}


def test_health_returns_503_when_database_unreachable(app: FastAPI) -> None:
    app.state.db.read_engine = create_engine("sqlite:////nonexistent-hisaab-dir/x.db")
    with TestClient(app) as client:
        resp: Response = client.get("/health")
    assert resp.status_code == 503
    assert resp.json() == {"status": "error", "database": "unreachable"}
