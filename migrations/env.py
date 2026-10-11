"""Alembic environment.

Three deliberate choices:

1. render_as_batch=True (tech-design §9.3). SQLite's ALTER TABLE cannot drop or
   alter columns; batch mode rewrites such changes as "create new table, copy,
   drop old, rename". It is configured from day one, not discovered at the
   first column change.

2. Migrations run with foreign_keys=OFF, then verify with foreign_key_check.
   Batch mode drops and recreates tables. With foreign keys ON, dropping a
   parent such as `transactions` performs an implicit DELETE that cascades
   (ON DELETE CASCADE) into transaction_edits and transaction_sources and
   erases the user's edits. Turning enforcement off for the migration and
   checking integrity afterwards is SQLite's documented procedure for schema
   changes (https://sqlite.org/lang_altertable.html, "Making Other Kinds Of
   Table Schema Changes"). The application engine always has foreign_keys=ON.

3. The engine comes from hisaab.db.make_engine, so the Windows-mount guard and
   WAL apply here too. A failed migration raises, run.sh's `set -e` stops, and
   the server never starts on a half-migrated schema (§9.3).
"""

from collections.abc import Sequence
from logging.config import fileConfig
from pathlib import Path
from typing import Any

from alembic import context
from alembic.config import Config
from sqlalchemy import Engine, MetaData, Row, text

from hisaab.config import Settings
from hisaab.db import make_engine
from hisaab.models import Base

config: Config = context.config
if config.config_file_name is not None and not config.attributes.get("skip_logging_config"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Tests inject these through config.attributes; normal runs read .env.
DATABASE_PATH: Path = Path(config.attributes.get("database_path") or Settings().database_path)
# The models are the source of truth that autogenerate compares against.
target_metadata: MetaData = config.attributes.get("target_metadata", Base.metadata)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without touching a database (`alembic upgrade --sql`)."""
    context.configure(
        url=f"sqlite:///{DATABASE_PATH}",
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine: Engine = make_engine(DATABASE_PATH, enforce_foreign_keys=False)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
            with context.begin_transaction():
                context.run_migrations()
            violations: Sequence[Row[Any]] = connection.execute(text("PRAGMA foreign_key_check")).fetchall()
            if violations:
                raise RuntimeError(f"Migration left foreign key violations: {violations[:10]}")
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
