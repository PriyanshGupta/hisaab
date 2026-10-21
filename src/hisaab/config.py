"""Application settings, loaded from the project-scoped .env file.

Secrets live only in <project root>/.env (gitignored; template in .env.example).
pydantic-settings reads that file itself and never copies values into
os.environ, so loading settings cannot leak a key to child processes.

Why the key is HISAAB_ANTHROPIC_API_KEY and not ANTHROPIC_API_KEY:
Claude Code authenticates with ANTHROPIC_API_KEY whenever it finds it in its
environment, which would silently bill development work to the API credits
meant for the running app (PRD §8.3, tech-design §9.1). direnv or an editor
that "loads .env into terminals" can export this file into a shell; a prefixed
name stays inert to Claude Code even if that happens.
"""

from pathlib import Path
from typing import ClassVar

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/hisaab/config.py -> parents[2] is the repository root.
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="HISAAB_",
        env_ignore_empty=True,  # a blank line copied from .env.example means "unset"
        extra="ignore",
        validate_default=True,  # so the default database_path is anchored too
    )

    # Relative paths resolve against PROJECT_ROOT, never the cwd, so a systemd
    # unit or `cd /` cannot silently create a second database somewhere else.
    database_path: Path = Path("data/hisaab.db")
    host: str = "0.0.0.0"  # LAN-reachable for phone access (tech-design §9.6)
    port: int = 8765  # the OAuth loopback callback targets this port (§2.3, §5.5)
    # The built SPA (npm run build in frontend/). Served at / with a client-route fallback.
    frontend_dist: Path = Path("frontend/dist")
    # Where a promoted template's three examples are exported, redacted (promotion loop, tech-design §4.10).
    fixtures_dir: Path = Path("fixtures/emails")
    # Backups (tech-design §9.2): a separate folder, ideally synced or external storage. Unlike the live database,
    # a finished backup may live on the Windows mount (e.g. a OneDrive folder under /mnt/c).
    backup_dir: Path = Path("data/backups")

    # Optional until the slice that needs each one makes it required.
    anthropic_api_key: SecretStr | None = None
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: SecretStr | None = None
    encryption_key: SecretStr | None = None
    session_secret: SecretStr | None = None

    # LLM tiers (tech-design §4.11): any server speaking the Anthropic Messages API.
    llm_base_url: str = "https://api.anthropic.com"
    email_model: str = "claude-haiku-5-5"
    statement_model: str = "claude-sonnet-5-5"  # tier 3: unknown statement layouts (tech-design §4.11)
    template_model: str = "claude-sonnet-5-5"  # template proposals (§4.10 step 2; §4.11)

    # The promotion loop (§4.10). Off: a candidate that passes replay waits for an admin's approval.
    auto_promote_templates: bool = False

    @field_validator("database_path", "frontend_dist", "fixtures_dir", "backup_dir")
    @classmethod
    def _anchor_to_project_root(cls, value: Path) -> Path:
        value = value.expanduser()
        return value if value.is_absolute() else PROJECT_ROOT / value
