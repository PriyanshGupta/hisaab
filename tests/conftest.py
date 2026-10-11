from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Connection, Engine

from hisaab.config import PROJECT_ROOT, Settings
from hisaab.db import Database, make_engine


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings isolated from the developer's real .env, on a throwaway database."""
    return Settings(
        _env_file=None,
        database_path=tmp_path / "test.db",
        session_secret="test-session-secret-0123456789abcdef",
    )


def alembic_config(db_path: Path) -> Config:
    cfg: Config = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.attributes["database_path"] = db_path
    cfg.attributes["skip_logging_config"] = True  # keep pytest's log capture intact
    return cfg


def migrate(db_path: Path, revision: str = "head") -> None:
    command.upgrade(alembic_config(db_path), revision)


@pytest.fixture
def migrated_engine(settings: Settings) -> Iterator[Engine]:
    """Engine on a database built by the real migrations, exactly as production builds it."""
    migrate(settings.database_path)
    engine: Engine = make_engine(settings.database_path)
    yield engine
    engine.dispose()


@pytest.fixture
def db(settings: Settings, migrated_engine: Engine) -> Iterator[Database]:
    """The app's Database (write pool of one, read-only pool) on the migrated throwaway database."""
    database: Database = Database(settings.database_path)
    yield database
    database.dispose()


@pytest.fixture
def conn(migrated_engine: Engine) -> Iterator[Connection]:
    with migrated_engine.connect() as connection:
        yield connection


@pytest.fixture
def app(settings: Settings, migrated_engine: Engine) -> FastAPI:
    """The real app on a migrated throwaway database."""
    from hisaab.main import create_app

    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:

    with TestClient(app) as c:
        yield c


def make_user(app: FastAPI, username: str, password: str, role: str = "member",
              display_name: str | None = None) -> str:
    """Create a user through the users repository (the CLI/admin path), committed."""
    from hisaab.auth import users

    with app.state.db.write_session() as s:
        return users.create_user(s, username, display_name or username.title(), password, role).id


def login(client: TestClient, username: str, password: str) -> Response:
    return client.post("/api/auth/login", json={"username": username, "password": password})
