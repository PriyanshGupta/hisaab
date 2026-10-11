"""No row may point at another user's data. Runs the scan over a database
filled through the real API by two users."""

from fastapi.testclient import TestClient

from tests import factories as f
from tests.conftest import login, make_user
from tests.integrity import cross_user_violations

ANCHOR = "2026-07-11"


def _fill(client, prefix: str, last4s: list[str]):
    for i, last4 in enumerate(last4s):
        acct = client.post("/api/accounts", json={
            "type": "bank", "display_name": f"{prefix} bank {i}", "last4": last4,
            "opening_balance_paise": 100000, "opening_balance_date": ANCHOR,
        }).json()
        client.post(f"/api/accounts/{acct['id']}/identifiers", json={"kind": "vpa", "identifier": f"{prefix}{i}@ybl"})
    client.post(f"/api/accounts/{acct['id']}/archive")  # archiving must not break ownership either


def test_two_users_data_never_crosses(app, migrated_engine):
    make_user(app, "asha", "asha password 1")
    make_user(app, "ravi", "ravi password 1")
    with TestClient(app) as asha, TestClient(app) as ravi:
        login(asha, "asha", "asha password 1")
        login(ravi, "ravi", "ravi password 1")
        _fill(asha, "asha", ["5512", "2218"])
        _fill(ravi, "ravi", ["5512", "8830"])  # same last4 as asha on purpose
    with migrated_engine.connect() as conn:
        assert cross_user_violations(conn) == {}


def test_the_scan_catches_a_planted_violation(conn):
    """Proves the scan can fail: an identifier owned by one user on another's account."""
    asha_account = f.account(conn)
    ravi = f.user(conn)
    planted = f.account_identifier(conn, asha_account["id"], ravi["id"], kind="vpa", identifier="planted@ybl")
    assert cross_user_violations(conn) == {"identifier belongs to another user's account": [planted["id"]]}
