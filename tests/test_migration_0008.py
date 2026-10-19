"""Migration 0008 (slice 17): who wrote a payee rule, auto-categorize's memory, an optional opening balance,
and the auto-categorize threshold."""

from typing import Any

import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import IntegrityError

from hisaab.config import Settings
from hisaab.db import Database
from hisaab.tunables import tunable
from tests import factories as f
from tests.conftest import alembic_config, migrate

INDEX_SQL: str = ("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name IN ('accounts', 'payee_categories') "
                  "ORDER BY name")


def test_a_rule_says_who_wrote_it(conn: Connection) -> None:
    f.payee_category(conn, origin="llm", batch_id="b-1")
    with pytest.raises(IntegrityError, match="ck_payee_categories_origin"):
        f.payee_category(conn, origin="robot")


def test_an_opening_balance_is_both_or_neither(conn: Connection) -> None:
    f.account(conn, opening_balance=None, opening_balance_date=None)  # no anchor: allowed (ruling 2026-10-12)
    with pytest.raises(IntegrityError, match="ck_accounts_anchor_both_or_neither"):
        f.account(conn, opening_balance=100, opening_balance_date=None)


def test_suggestions_remember_one_answer_per_payee(conn: Connection) -> None:
    u: str = f.user(conn)["id"]
    p: str = f.payee(conn)["id"]
    row: dict[str, Any] = {"user_id": u, "payee_id": p, "confidence_percent": 40, "categories_hash": "h",
                           "batch_id": "b", "created_at": "2026-10-12T00:00:00.000Z"}
    insert: str = ("INSERT INTO payee_suggestions (user_id, payee_id, category_id, confidence_percent, categories_hash,"
                   " batch_id, created_at) VALUES (:user_id, :payee_id, NULL, :confidence_percent, :categories_hash,"
                   " :batch_id, :created_at)")
    conn.execute(text(insert), row)
    with pytest.raises(IntegrityError):
        conn.execute(text(insert), row)


def test_the_threshold_is_80_percent(db: Database) -> None:
    with db.read_session() as s:
        assert tunable(s, "auto_categorize_min_confidence_percent") == 80


def _snapshot(engine: Engine) -> dict[str, Any]:
    with engine.connect() as c:
        return {"indexes": c.execute(text(INDEX_SQL)).all(),
                "rows": [c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one()
                         for t in ("accounts", "payee_categories", "transactions")],
                "effective": c.execute(text("SELECT COUNT(*) FROM transactions_effective")).scalar_one()}


def test_accounts_rules_transactions_the_view_and_indexes_survive(settings: Settings) -> None:
    migrate(settings.database_path, "0007")
    engine: Engine = create_engine(f"sqlite:///{settings.database_path}")
    with engine.begin() as c:
        u: str = f.user(c)["id"]
        bank: str = f.account(c, u, last4="1111")["id"]
        f.account(c, u, last4="2222", archived_at="2026-10-01T00:00:00.000Z")
        f.transaction(c, u, from_account_id=bank)
        c.execute(text("INSERT INTO payee_categories (user_id, payee_id, category_id, created_at, updated_at) "
                       "VALUES (:u, :p, :c, 'x', 'x')"), {"u": u, "p": f.payee(c)["id"], "c": f.category(c, u)["id"]})
    before: dict[str, Any] = _snapshot(engine)
    migrate(settings.database_path, "0008")
    after: dict[str, Any] = _snapshot(engine)
    assert after == before and before["rows"] == [2, 1, 1]
    with engine.begin() as c:  # the partial unique index still frees an archived account's last4
        f.account(c, u, last4="2222")
        with pytest.raises(IntegrityError):
            f.account(c, u, last4="1111")
    engine.dispose()


def test_downgrade_refuses_while_an_account_has_no_opening_balance(settings: Settings) -> None:
    from alembic import command
    migrate(settings.database_path, "0008")
    engine: Engine = create_engine(f"sqlite:///{settings.database_path}")
    with engine.begin() as c:
        f.account(c, opening_balance=None, opening_balance_date=None)
    engine.dispose()
    with pytest.raises(RuntimeError, match="1 account has no opening balance"):
        command.downgrade(alembic_config(settings.database_path), "0007")
