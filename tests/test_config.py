import os

from hisaab.config import PROJECT_ROOT, Settings


def test_project_root_is_the_repo_root():
    assert (PROJECT_ROOT / "pyproject.toml").is_file()


def test_starts_on_defaults_when_no_env_file_exists(tmp_path):
    s = Settings(_env_file=tmp_path / "does-not-exist")
    assert s.database_path == PROJECT_ROOT / "data" / "hisaab.db"
    assert s.host == "0.0.0.0"
    assert s.port == 8765
    assert s.anthropic_api_key is None


def test_relative_database_path_is_anchored_to_project_root_not_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = Settings(_env_file=None, database_path="data/elsewhere.db")
    assert s.database_path == PROJECT_ROOT / "data" / "elsewhere.db"


def test_reads_values_from_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("HISAAB_PORT=9001\nHISAAB_SESSION_SECRET=s3cret\n")
    s = Settings(_env_file=env)
    assert s.port == 9001
    assert s.session_secret.get_secret_value() == "s3cret"


def test_blank_values_mean_unset(tmp_path):
    env = tmp_path / ".env"
    env.write_text("HISAAB_ANTHROPIC_API_KEY=\n")
    assert Settings(_env_file=env).anthropic_api_key is None


def test_loading_env_file_never_touches_process_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("HISAAB_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("HISAAB_ANTHROPIC_API_KEY=sk-test-not-real\n")

    s = Settings(_env_file=env)

    assert s.anthropic_api_key.get_secret_value() == "sk-test-not-real"
    assert "HISAAB_ANTHROPIC_API_KEY" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_bare_ANTHROPIC_API_KEY_is_deliberately_ignored(monkeypatch):
    # The app key is HISAAB_ANTHROPIC_API_KEY; see config.py docstring for why.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-claude-code-would-use-this")
    assert Settings(_env_file=None).anthropic_api_key is None


def test_env_example_lists_exactly_the_settings_fields():
    lines = (PROJECT_ROOT / ".env.example").read_text().splitlines()
    keys = {l.split("=", 1)[0].strip() for l in lines if l.strip() and not l.lstrip().startswith("#")}
    assert keys == {f"HISAAB_{name.upper()}" for name in Settings.model_fields}


def test_env_example_is_loadable_as_is():
    example = PROJECT_ROOT / ".env.example"
    assert example.is_file()  # pydantic-settings silently ignores a missing env file
    s = Settings(_env_file=example)
    assert s.database_path == PROJECT_ROOT / "data" / "hisaab.db"
    assert s.anthropic_api_key is None  # blank placeholders stay unset
