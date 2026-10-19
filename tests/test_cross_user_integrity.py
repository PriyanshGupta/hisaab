"""No row may point at another user's data.

1. The scan passes over a database filled through the real API by two users.
2. Every check can fail: each one catches a violation planted just for it.
3. Every foreign key between user-scoped tables has a check (schema guard).
"""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, text

from hisaab.models import Base
from tests import factories as f
from tests.conftest import login, make_user
from tests.integrity import CHECKS, Check, cross_user_violations

ANCHOR: str = "2026-07-11"


def _fill(client: TestClient, prefix: str, last4s: list[str]) -> None:
    ids: list[str] = []
    for i, last4 in enumerate(last4s):
        acct: Any = client.post("/api/accounts", json={
            "type": "bank", "display_name": f"{prefix} bank {i}", "last4": last4,
            "opening_balance_paise": 100000, "opening_balance_date": ANCHOR,
        }).json()
        client.post(f"/api/accounts/{acct['id']}/identifiers", json={"kind": "vpa", "identifier": f"{prefix}{i}@ybl"})
        ids.append(acct["id"])
    wallet: Any = client.post("/api/accounts", json={"type": "cash", "display_name": f"{prefix} wallet",
                                                "opening_balance_paise": 0, "opening_balance_date": ANCHOR}).json()["id"]
    spend: Any = client.post("/api/transactions", json={"kind": "expense", "amount_paise": 25000,
                                                   "transaction_date": "2026-10-01", "from_account_id": wallet,
                                                   "payee": "Tea"}).json()
    atm: Any = client.post("/api/transactions", json={"kind": "expense", "amount_paise": 500000,
                                                 "transaction_date": "2026-10-02", "from_account_id": ids[0],
                                                 "payee": "ATM"}).json()
    client.patch(f"/api/transactions/{spend['id']}", json={"amount_paise": 30000})
    client.patch(f"/api/transactions/{atm['id']}", json={"kind": "transfer", "to_account_id": wallet})
    client.post(f"/api/accounts/{ids[-1]}/archive")


def test_two_users_data_never_crosses(app: FastAPI, migrated_engine: Engine) -> None:
    make_user(app, "asha", "asha password 1")
    make_user(app, "ravi", "ravi password 1")
    with TestClient(app) as asha, TestClient(app) as ravi:
        login(asha, "asha", "asha password 1")
        login(ravi, "ravi", "ravi password 1")
        _fill(asha, "asha", ["5512", "2218"])
        _fill(ravi, "ravi", ["5512", "8830"])  # same last4 as asha on purpose
    with migrated_engine.connect() as conn:
        counts: dict[str, Any | None] = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                  for t in ("accounts", "transactions", "transaction_edits", "sources")}
        assert counts == {"accounts": 6, "transactions": 4, "transaction_edits": 6, "sources": 4}  # not vacuous
        assert cross_user_violations(conn) == {}


# --- each check can fail -------------------------------------------------------------
# One planting per check: a row owned by user A pointing at user B's data.

def _two_users(conn: Connection) -> tuple[str, str]:
    return f.user(conn)["id"], f.user(conn)["id"]


def _plant(conn: Connection, name: str) -> None:
    a, b = _two_users(conn)
    b_account: str = f.account(conn, b)["id"]
    b_source: str = f.source(conn, b)["id"]
    b_txn: str = f.transaction(conn, user_id=b, source_id=b_source)["id"]
    if name.startswith("account_identifiers.account_id"):
        f.account_identifier(conn, b_account, a)
    elif name.startswith("transactions.from_account_id"):
        f.transaction(conn, user_id=a, from_account_id=b_account)
    elif name.startswith("transactions.to_account_id"):
        f.transaction(conn, user_id=a, kind="income", to_account_id=b_account)
    elif name.startswith("transactions.primary_source_id"):
        f.transaction(conn, user_id=a, source_id=b_source)
    elif name.startswith("transactions.reverses_transaction_id"):
        f.transaction(conn, user_id=a, reverses_transaction_id=b_txn)
    elif name.startswith("transactions.refunds_transaction_id"):
        f.transaction(conn, user_id=a, refunds_transaction_id=b_txn)
    elif name.startswith("transaction_edits.transaction_id"):
        f.transaction_edit(conn, b_txn, a)
    elif name.startswith("payee_categories.category_id"):
        f.payee_category(conn, user_id=a, category_id=f.category(conn, b)["id"])
    elif name.startswith("raw_payee_suggestions.category_id"):
        f.raw_payee_suggestion(conn, user_id=a, category_id=f.category(conn, b)["id"])
    elif name.startswith("payee_suggestions.category_id"):
        f.payee_suggestion(conn, user_id=a, category_id=f.category(conn, b)["id"])
    elif name.startswith("balance_observations.account_id"):
        f.balance_observation(conn, user_id=a, account_id=b_account)
    elif name.startswith("balance_observations.source_id"):
        f.balance_observation(conn, user_id=a, account_id=f.account(conn, a)["id"], source_id=b_source)
    elif name.startswith("balance_deltas.account_id"):
        obs: dict[str, Any] = f.balance_observation(conn, user_id=a, account_id=f.account(conn, a)["id"])
        f.balance_delta(conn, user_id=a, account_id=b_account, observation_id=obs["id"])
    elif name.startswith("balance_deltas.observation_id"):
        obs = f.balance_observation(conn, user_id=b, account_id=b_account)
        f.balance_delta(conn, user_id=a, account_id=f.account(conn, a)["id"], observation_id=obs["id"])
    elif name.startswith("balance_deltas.adjustment_transaction_id"):
        obs = f.balance_observation(conn, user_id=a, account_id=f.account(conn, a)["id"])
        f.balance_delta(conn, user_id=a, account_id=obs["account_id"], observation_id=obs["id"],
                        adjustment_transaction_id=b_txn)
    elif name.startswith("statement_staging.account_id"):
        f.statement_staging(conn, user_id=a, account_id=b_account)
    elif name.startswith("transaction_sources"):
        f.transaction_source(conn, f.transaction(conn, user_id=a)["id"], b_source)
    elif "another user's account" in name:
        f.transaction_edit(conn, f.transaction(conn, user_id=a)["id"], a, field="to_account_id", new_value=b_account)
    elif "another user's category" in name:
        f.transaction_edit(conn, f.transaction(conn, user_id=a)["id"], a, field="category_override_id",
                           new_value=f.category(conn, b)["id"])
    else:
        raise AssertionError(f"no planting for check: {name}")


@pytest.mark.parametrize("check", CHECKS, ids=[c.name for c in CHECKS])
def test_each_check_catches_its_planted_violation(conn: Connection, check: Check) -> None:
    _plant(conn, check.name)
    assert list(cross_user_violations(conn)) == [check.name]


# --- schema guard ---------------------------------------------------------------------

def test_every_foreign_key_between_user_scoped_tables_is_checked() -> None:
    user_scoped: set[str] = {t.name for t in Base.metadata.tables.values() if "user_id" in t.c} - {"settings"}
    covered: set[tuple[str, str]] = {pair for check in CHECKS for pair in check.covers}
    needed: set[tuple[str, str]] = {  # every FK column pointing into a user-scoped table
        (table.name, fk.parent.name)
        for table in Base.metadata.tables.values()
        for fk in table.foreign_keys
        if fk.column.table.name in user_scoped
    }
    assert needed - covered == set(), "add a check to tests/integrity.py for these references"
