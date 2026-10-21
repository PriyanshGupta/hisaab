"""Structure of the migrated schema against tech-design §3 (as amended 2026-10-10)."""

import json
import re
from typing import Any, Sequence

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, CursorResult, Engine, Row, text

from hisaab.config import Settings
from tests.conftest import alembic_config

GLOBAL_TABLES: set[str] = {
    "payees", "parser_templates", "extraction_cache",
    "format_sightings", "normalization_patterns", "senders",
    "correction_dismissals", "resync_notices",  # slice 24 (migration 0012)
}
ALL_TABLES: set[str] = GLOBAL_TABLES | {
    # §3.2 core
    "users", "accounts", "account_identifiers", "sources", "transactions",
    "transaction_sources", "transaction_edits", "payee_categories", "categories",
    "balance_observations", "balance_deltas",
    # §3.3 parsing and ingestion
    "sender_candidates", "gmail_connections", "statement_staging",
    # §3.4 operational
    "jobs", "sessions", "settings",
    # slice 17 (migration 0008): auto-categorize's memory; (0009) numbers that aren't the user's accounts
    "payee_suggestions", "external_identifiers", "raw_payee_suggestions",
}

# §3.8: name -> (table, [(column, descending)], partial-predicate or None)
EXPECTED_INDEXES: dict[str, tuple[str, list[tuple[str, int]], str | None]] = {
    "ix_txn_user_date": ("transactions", [("user_id", 0), ("transaction_date", 0)], None),
    "ix_txn_user_status": ("transactions", [("user_id", 0), ("status", 0)], "status = 'needs_review'"),
    "ix_txn_user_payee": ("transactions", [("user_id", 0), ("payee_id", 0)], None),
    "ix_txn_from_acct": ("transactions", [("from_account_id", 0)], None),
    "ix_txn_to_acct": ("transactions", [("to_account_id", 0)], None),
    "ix_edits_txn": ("transaction_edits", [("transaction_id", 0)], None),
    "ix_txn_user_ref": ("transactions", [("user_id", 0), ("reference_number", 0)], "reference_number IS NOT NULL"),
    "ix_txn_dedup_fb": ("transactions", [("user_id", 0), ("amount", 0), ("transaction_date", 0)], None),
    "ix_sources_state": ("sources", [("processing_state", 0)], "processing_state IN ('pending_llm', 'failed')"),
    "ix_sources_seq": ("sources", [("ingest_seq", 0)], None),
    "ix_tsrc_source": ("transaction_sources", [("source_id", 0)], None),
    "ix_acctid_lookup": ("account_identifiers", [("user_id", 0), ("identifier", 0)], None),
    "ix_normpat_priority": ("normalization_patterns", [("priority", 1), ("id", 0)], None),
    "ix_jobs_user_state": ("jobs", [("user_id", 0), ("state", 0), ("created_at", 1)], None),
    "ix_balobs_acct_date": ("balance_observations", [("account_id", 0), ("observed_at", 1)], None),
    "ix_deltas_open": ("balance_deltas", [("user_id", 0), ("status", 0)], "status = 'open'"),
}

EXPECTED_SETTINGS: dict[str, Any] = {
    "backfill_window_months": 3,
    "sync_min_lookback_hours": 48,
    "sync_interval_hours": 6,
    "discovery_sweep_interval_days": 7,
    "dedup_date_window_days": 3,
    "dedup_primary_key": "reference_number",
    "dedup_amount_tolerance_paise": 0,
    "transfer_match_window_days": 5,
    "statement_email_match_window_days": 3,
    "edit_conflict_fields": ["amount", "from_account_id", "to_account_id", "transaction_date"],
    "large_amount_threshold_paise": 2500000,
    "template_promotion_sightings": 3,
    "template_demotion": {"consecutive_failures": 3, "window_days": 30},
    "balance_delta_tolerance_paise": 100,
    "recurring_detection": {"min_occurrences": 3, "interval_tolerance_days": 4, "amount_tolerance_percent": 10},
    "pattern_timeout_ms": 100,
    "auto_categorize_min_confidence_percent": 80,  # slice 17 (0008): not a PRD §10.1 row, the user's brief
}

# Every row of PRD §10.1, in order, mapped to the settings key that carries it.
# Sweep lookback deliberately shares the backfill key (tech-design §5.3).
PRD_10_1_ROWS: dict[str, str] = {
    "Backfill window": "backfill_window_months",
    "Sync minimum lookback": "sync_min_lookback_hours",
    "Automatic sync interval": "sync_interval_hours",
    "Discovery sweep interval": "discovery_sweep_interval_days",
    "Discovery sweep lookback": "backfill_window_months",
    "Dedup date window": "dedup_date_window_days",
    "Dedup primary key": "dedup_primary_key",
    "Dedup amount tolerance (fallback)": "dedup_amount_tolerance_paise",
    "Transfer match window": "transfer_match_window_days",
    "Statement/email match window": "statement_email_match_window_days",
    "Edit conflict tolerance": "edit_conflict_fields",
    "Large-amount review threshold": "large_amount_threshold_paise",
    "Template promotion threshold": "template_promotion_sightings",
    "Template demotion threshold": "template_demotion",
    "Balance delta tolerance": "balance_delta_tolerance_paise",
    "Recurring detection": "recurring_detection",
    "Pattern execution timeout": "pattern_timeout_ms",
}


def _objects(conn: Connection, kind: str) -> set[str]:
    rows: CursorResult[Any] = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type = :k AND name NOT LIKE 'sqlite_%' AND name <> 'alembic_version'"),
        {"k": kind},
    )
    return {r[0] for r in rows}


def test_migration_creates_exactly_the_28_tables(conn: Connection) -> None:
    assert _objects(conn, "table") == ALL_TABLES
    assert len(ALL_TABLES) == 28


def test_migration_creates_the_effective_view(conn: Connection) -> None:
    assert _objects(conn, "view") == {"transactions_effective"}


def test_only_documented_tables_lack_a_user_id_column(conn: Connection) -> None:
    lacking: set[str] = {
        t for t in ALL_TABLES
        if "user_id" not in {r[1] for r in conn.execute(text(f"PRAGMA table_info({t})"))}
    }
    # users is the user; transaction_sources is scoped through its transaction;
    # sender_candidates names it discovered_by_user_id.
    assert lacking == GLOBAL_TABLES | {"users", "transaction_sources", "sender_candidates"}


def _normalize_sql(s: str) -> str:
    return re.sub(r"\s+", " ", s).replace('"', "")


def test_every_index_from_tech_design_3_8(conn: Connection) -> None:
    named: dict[Any, tuple[Any, Any]] = {
        r[0]: (r[1], r[2])
        for r in conn.execute(text("SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' AND name LIKE 'ix_%'"))
    }
    assert set(named) == set(EXPECTED_INDEXES)
    for name, (table, columns, predicate) in EXPECTED_INDEXES.items():
        actual_table, sql = named[name]
        assert actual_table == table, name
        key_columns: list[tuple[Any, Any]] = [
            (r[2], r[3]) for r in conn.execute(text(f"PRAGMA index_xinfo({name})")) if r[5] == 1  # r[5]: key column
        ]
        assert key_columns == columns, name
        partial: Any = next(r[4] for r in conn.execute(text(f"PRAGMA index_list({table})")) if r[1] == name)
        assert partial == (predicate is not None), name
        if predicate:
            assert f"WHERE {predicate}" in _normalize_sql(sql), (name, sql)


def test_models_match_migration(conn: Connection) -> None:
    from hisaab.models import Base

    diff: Any = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_downgrade_then_upgrade_round_trips(migrated_engine: Engine, settings: Settings) -> None:
    cfg: Config = alembic_config(settings.database_path)
    command.downgrade(cfg, "base")
    with migrated_engine.connect() as c:
        assert _objects(c, "table") == set()
        assert _objects(c, "view") == set()
    command.upgrade(cfg, "head")
    with migrated_engine.connect() as c:
        assert _objects(c, "table") == ALL_TABLES


def _contains_float(value: object) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(_contains_float(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_float(v) for v in value)
    return False


def test_settings_seeded_with_every_prd_10_1_tunable(conn: Connection) -> None:
    rows: Sequence[Row[Any]] = conn.execute(text("SELECT key, user_id, value, updated_at FROM settings")).all()
    assert {r.key: json.loads(r.value) for r in rows} == EXPECTED_SETTINGS
    assert all(r.user_id == "" for r in rows)  # app-level rows
    assert all(r.updated_at.endswith("Z") for r in rows)
    assert not any(_contains_float(json.loads(r.value)) for r in rows)  # no floats, ever


def test_every_prd_10_1_row_maps_to_a_seeded_key() -> None:
    assert len(PRD_10_1_ROWS) == 17
    assert set(PRD_10_1_ROWS.values()) == set(EXPECTED_SETTINGS) - {"auto_categorize_min_confidence_percent"}
