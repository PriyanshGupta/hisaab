import pytest

from hisaab.config import Settings


@pytest.fixture
def settings(tmp_path):
    """Settings isolated from the developer's real .env, on a throwaway database."""
    return Settings(_env_file=None, database_path=tmp_path / "test.db")
