"""Declarative base, constraint naming, and column helpers shared by all models.

Conventions (tech-design §3.1):
- Primary keys are UUID strings (TEXT); `new_id()` fills them on insert.
- Timestamps are ISO-8601 UTC strings with a Z suffix; `utc_now()` fills them.
  Fixed width (always milliseconds) so that string order equals time order.
- Dates are 'YYYY-MM-DD' TEXT; money is BIGINT integer paise; JSON is TEXT.
- Soft delete is an `archived_at` timestamp, never a boolean.

The models are the source of truth for table *structure*. Migrations are
generated from them and then frozen; tests/test_schema_structure.py fails if
the two ever disagree.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, Integer, LargeBinary, MetaData, Text, text
from sqlalchemy.orm import DeclarativeBase, MappedColumn, mapped_column

# Every constraint gets a deterministic name. Alembic's batch mode on SQLite can
# only drop or alter a constraint it can name, so unnamed constraints would be
# effectively permanent. CHECK constraints must be given a short `name`, which
# this convention expands to ck_<table>_<name>; that full name is also what
# SQLite prints in "CHECK constraint failed: ..." errors.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Plain `str` columns are TEXT (not VARCHAR); money columns opt into BigInteger explicitly.
    type_annotation_map = {str: Text, int: Integer, bytes: LargeBinary}


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> str:
    """'YYYY-MM-DDTHH:MM:SS.mmmZ' in UTC."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def one_of(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    """CHECK (column IN (...)), the schema's enum representation (portable, no native enums)."""
    allowed = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


def id_column() -> MappedColumn:
    return mapped_column(Text, primary_key=True, default=new_id)


def created_column() -> MappedColumn:
    return mapped_column(Text, default=utc_now)


def updated_column() -> MappedColumn:
    return mapped_column(Text, default=utc_now, onupdate=utc_now)


def partial_index_where(predicate: str) -> dict:
    """Index kwargs for a partial index, on both SQLite and (future) Postgres."""
    return {"sqlite_where": text(predicate), "postgresql_where": text(predicate)}
