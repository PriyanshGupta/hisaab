"""Migration 0006: a transfer may leave its paying account unknown (ruling 2026-10-11).

A card's "payment received" email names the card but not the account that paid
it. That is a card bill payment, so a transfer, with from_account_id NULL until
a matching bank debit (or the user) fills it in. The old CHECK demanded both legs.
"""

from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text

from hisaab.config import Settings
from tests import factories as f
from tests.conftest import alembic_config, migrate

INDEX_SQL: str = "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'transactions' ORDER BY name"


def _seed(engine: Engine) -> None:
    with engine.begin() as c:
        u: str = f.user(c, id="u")["id"]
        bank: str = f.account(c, u, id="bank")["id"]
        card: str = f.account(c, u, id="card", type="credit_card")["id"]
        txn: str = f.transaction(c, u, id="t", amount=500, kind="transfer", from_account_id=bank,
                                 to_account_id=card)["id"]
        f.transaction_source(c, txn, f.transaction(c, u)["primary_source_id"])
        f.transaction_edit(c, txn, u, field="amount", prior_value="500", new_value="600")


def _snapshot(engine: Engine) -> dict[str, Any]:
    with engine.connect() as c:
        return {"indexes": c.execute(text(INDEX_SQL)).all(),
                "rows": [c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one()
                         for t in ("transactions", "transaction_sources", "transaction_edits")],
                "effective": c.execute(text("SELECT amount FROM transactions_effective WHERE id = 't'")).scalar_one()}


def test_rows_links_edits_the_view_and_indexes_survive(settings: Settings) -> None:
    migrate(settings.database_path, "0005")
    engine: Engine = create_engine(f"sqlite:///{settings.database_path}")
    _seed(engine)
    before: dict[str, Any] = _snapshot(engine)
    migrate(settings.database_path, "0006")
    assert _snapshot(engine) == before and before["rows"] == [2, 1, 1] and before["effective"] == 600
    engine.dispose()


def test_downgrade_refuses_while_a_transfer_has_no_paying_account(settings: Settings) -> None:
    from alembic import command
    migrate(settings.database_path, "0006")
    engine: Engine = create_engine(f"sqlite:///{settings.database_path}")
    _seed(engine)
    with engine.begin() as c:
        c.execute(text("UPDATE transactions SET from_account_id = NULL"))
    engine.dispose()
    with pytest.raises(RuntimeError, match="1 transfer has no paying account"):
        command.downgrade(alembic_config(settings.database_path), "0005")
