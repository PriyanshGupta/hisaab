import pytest
from alembic import command
from alembic.config import Config

from hisaab.config import PROJECT_ROOT, Settings
from hisaab.db import make_engine


@pytest.fixture
def settings(tmp_path):
    """Settings isolated from the developer's real .env, on a throwaway database."""
    return Settings(_env_file=None, database_path=tmp_path / "test.db")


def alembic_config(db_path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.attributes["database_path"] = db_path
    cfg.attributes["skip_logging_config"] = True  # keep pytest's log capture intact
    return cfg


def migrate(db_path, revision: str = "head") -> None:
    command.upgrade(alembic_config(db_path), revision)


@pytest.fixture
def migrated_engine(settings):
    """Engine on a database built by the real migrations, exactly as production builds it."""
    migrate(settings.database_path)
    engine = make_engine(settings.database_path)
    yield engine
    engine.dispose()


@pytest.fixture
def conn(migrated_engine):
    with migrated_engine.connect() as connection:
        yield connection
