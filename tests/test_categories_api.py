"""/api/categories, /api/payees, payee rules, and categories on /api/transactions."""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

from tests.conftest import login, make_user


@pytest.fixture
def asha(app: FastAPI, client: TestClient) -> TestClient:
    make_user(app, "asha", "asha password 1")
    login(client, "asha", "asha password 1")
    return client


@pytest.fixture
def wallet(asha: TestClient) -> str:
    created: dict[str, Any] = asha.post("/api/accounts", json={"type": "cash", "display_name": "Wallet", "opening_balance_paise": 0,
                                            "opening_balance_date": "2026-07-01"}).json()
    return str(created["id"])


def cat_id(client: TestClient, name: str) -> str:
    return str(next(c["id"] for c in client.get("/api/categories").json() if c["name"] == name))


def enter(client: TestClient, wallet: str, payee: str, **kw: Any) -> dict[str, Any]:
    created: dict[str, Any] = client.post("/api/transactions", json={"kind": "expense", "amount_paise": 10000,
                                                  "transaction_date": "2026-10-05", "from_account_id": wallet,
                                                  "payee": payee, **kw}).json()
    return created


def test_requires_login(client: TestClient) -> None:
    for path in ("/api/categories", "/api/payees"):
        assert client.get(path).status_code == 401


def test_new_users_have_the_defaults(asha: TestClient) -> None:
    cats: Any = asha.get("/api/categories").json()
    assert [c["name"] for c in cats][:3] == ["Food & Dining", "Groceries", "Transport"]
    assert len(cats) == 10 and all(c["archived_at"] is None for c in cats)


def test_category_crud(asha: TestClient) -> None:
    created: Response = asha.post("/api/categories", json={"name": "Gifts"})
    assert created.status_code == 201
    gid: str = created.json()["id"]
    assert asha.patch(f"/api/categories/{gid}", json={"name": "Gifts & Festivals"}).json()["name"] == "Gifts & Festivals"
    assert asha.post(f"/api/categories/{gid}/archive").json()["archived_at"] is not None
    assert asha.post(f"/api/categories/{gid}/restore").json()["archived_at"] is None


def test_duplicate_category_is_409(asha: TestClient) -> None:
    resp: Response = asha.post("/api/categories", json={"name": "rent"})
    assert resp.status_code == 409 and resp.json()["detail"] == "You already have a category called Rent."


def test_payees_are_shared_by_everyone(app: FastAPI, asha: TestClient) -> None:
    asha.post("/api/payees", json={"name": "Swiggy", "type": "merchant"})
    make_user(app, "ravi", "ravi password 1")
    with TestClient(app) as ravi:
        login(ravi, "ravi", "ravi password 1")
        assert [p["name"] for p in ravi.get("/api/payees").json()] == ["Swiggy"]


def test_rules_are_per_user(app: FastAPI, asha: TestClient, wallet: str) -> None:
    t: dict[str, Any] = enter(asha, wallet, "Swiggy")
    payee: Any = t["payee_id"]
    assert asha.put(f"/api/payees/{payee}/category", json={"category_id": cat_id(asha, "Food & Dining")}).status_code == 200
    make_user(app, "ravi", "ravi password 1")
    with TestClient(app) as ravi:
        login(ravi, "ravi", "ravi password 1")
        assert ravi.get(f"/api/payees/{payee}").json()["category_id"] is None  # ravi has no rule for Swiggy
        assert ravi.put(f"/api/payees/{payee}/category",
                        json={"category_id": cat_id(asha, "Food & Dining")}).status_code == 404  # asha's category
    assert asha.get(f"/api/payees/{payee}").json()["category_id"] == cat_id(asha, "Food & Dining")


def test_transactions_carry_their_resolved_category(asha: TestClient, wallet: str) -> None:
    t: dict[str, Any] = enter(asha, wallet, "Swiggy")
    assert (t["payee_name"], t["category_id"], t["category_source"]) == ("Swiggy", None, None)
    food: str = cat_id(asha, "Food & Dining")
    asha.put(f"/api/payees/{t['payee_id']}/category", json={"category_id": food})
    assert asha.get(f"/api/transactions/{t['id']}").json()["category_id"] == food
    gifts: str = asha.post("/api/categories", json={"name": "Gifts"}).json()["id"]
    over: Any = asha.patch(f"/api/transactions/{t['id']}", json={"category_override_id": gifts}).json()
    assert (over["category_id"], over["category_source"]) == (gifts, "override")
    undone: Any = asha.post(f"/api/transactions/{t['id']}/revert", json={"fields": ["category_override_id"]}).json()
    assert (undone["category_id"], undone["category_source"]) == (food, "rule")


def test_manual_entry_category_pick(asha: TestClient, wallet: str) -> None:
    food: str = cat_id(asha, "Food & Dining")
    t: dict[str, Any] = enter(asha, wallet, "Tea stall", category_id=food)
    assert (t["category_id"], t["category_source"]) == (food, "rule")  # first pick learned as the rule


def test_another_users_category_is_404(app: FastAPI, asha: TestClient) -> None:
    rent: str = cat_id(asha, "Rent")
    make_user(app, "ravi", "ravi password 1")
    with TestClient(app) as ravi:
        login(ravi, "ravi", "ravi password 1")
        assert ravi.patch(f"/api/categories/{rent}", json={"name": "x"}).status_code == 404
        assert ravi.post(f"/api/categories/{rent}/archive").status_code == 404
