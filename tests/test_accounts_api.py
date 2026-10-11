"""/api/accounts: the HTTP surface of accounts and identifiers."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import login, make_user

ANCHOR = "2026-07-11"
BANK = {"type": "bank", "display_name": "HDFC Savings", "institution": "HDFC Bank", "last4": "5512",
        "opening_balance_paise": 8420000, "opening_balance_date": ANCHOR}
CARD = {"type": "credit_card", "display_name": "Regalia card", "institution": "HDFC Bank", "last4": "8830",
        "opening_balance_paise": 4700000, "opening_balance_date": ANCHOR}
CASH = {"type": "cash", "display_name": "Wallet", "opening_balance_paise": 350000, "opening_balance_date": ANCHOR}


@pytest.fixture
def asha(app, client):
    make_user(app, "asha", "asha password 1")
    login(client, "asha", "asha password 1")
    return client


def test_requires_login(client):
    assert client.get("/api/accounts").status_code == 401
    assert client.post("/api/accounts", json=BANK).status_code == 401


def test_create_all_three_types_and_list_them(asha):
    for body in (BANK, CARD, CASH):
        assert asha.post("/api/accounts", json=body).status_code == 201
    listed = asha.get("/api/accounts").json()
    assert [(a["type"], a["last4"], a["opening_balance_paise"]) for a in listed] == [
        ("bank", "5512", 8420000), ("credit_card", "8830", 4700000), ("cash", None, 350000),
    ]
    card = listed[1]
    assert card["identifiers"] == [{"id": card["identifiers"][0]["id"], "kind": "card_last4",
                                    "identifier": "8830", "removable": False}]


def test_duplicate_last4_is_409_with_a_clear_message(asha):
    asha.post("/api/accounts", json=BANK)
    resp = asha.post("/api/accounts", json=CARD | {"last4": "5512"})
    assert resp.status_code == 409
    assert resp.json() == {"detail": "You already have an account ending 5512: HDFC Savings."}


@pytest.mark.parametrize("change,status", [
    ({"opening_balance_paise": -4700000}, 422),   # card debt must be positive
    ({"opening_balance_paise": 4700000.5}, 422),  # paise are integers, never floats
    ({"opening_balance_paise": "4700000"}, 422),
    ({"last4": None}, 422),
    ({"type": "wallet"}, 422),
    ({"opening_balance_date": "11/07/2026"}, 422),
])
def test_invalid_card_is_refused(asha, change, status):
    resp = asha.post("/api/accounts", json=CARD | change)
    assert resp.status_code == status
    assert asha.get("/api/accounts").json() == []


def test_negative_card_message_explains_the_sign(asha):
    resp = asha.post("/api/accounts", json=CARD | {"opening_balance_paise": -4700000})
    assert "amount owed" in resp.json()["detail"]


def test_edit_renames_and_corrects_the_anchor(asha):
    acct = asha.post("/api/accounts", json=BANK).json()
    resp = asha.patch(f"/api/accounts/{acct['id']}", json={"display_name": "HDFC Salary",
                                                            "opening_balance_paise": 9000000})
    assert resp.status_code == 200
    assert (resp.json()["display_name"], resp.json()["opening_balance_paise"], resp.json()["institution"]) == (
        "HDFC Salary", 9000000, "HDFC Bank")  # unspecified fields unchanged


def test_identifier_lookup_is_case_insensitive(asha):
    acct = asha.post("/api/accounts", json=BANK).json()
    resp = asha.post(f"/api/accounts/{acct['id']}/identifiers", json={"kind": "vpa", "identifier": "upi@ybl"})
    assert resp.status_code == 201 and resp.json()["identifier"] == "upi@ybl"
    found = asha.get("/api/accounts/resolve", params={"identifier": "UPI@YBL"})
    assert found.status_code == 200 and found.json()["id"] == acct["id"]


def test_resolving_an_unknown_identifier_is_404(asha):
    assert asha.get("/api/accounts/resolve", params={"identifier": "nobody@ybl"}).status_code == 404


def test_archive_hides_the_account_and_stops_resolution(asha):
    acct = asha.post("/api/accounts", json=BANK).json()
    asha.post(f"/api/accounts/{acct['id']}/identifiers", json={"kind": "vpa", "identifier": "upi@ybl"})
    resp = asha.post(f"/api/accounts/{acct['id']}/archive")
    assert resp.status_code == 200 and resp.json()["archived_at"] is not None
    assert asha.get("/api/accounts").json() == []
    archived = asha.get("/api/accounts", params={"include_archived": "true"}).json()
    assert [a["id"] for a in archived] == [acct["id"]] and archived[0]["identifiers"] == []
    assert asha.get("/api/accounts/resolve", params={"identifier": "UPI@YBL"}).status_code == 404
    assert asha.get("/api/accounts/resolve", params={"identifier": "5512"}).status_code == 404


def test_archived_account_cannot_be_edited(asha):
    acct = asha.post("/api/accounts", json=BANK).json()
    asha.post(f"/api/accounts/{acct['id']}/archive")
    assert asha.patch(f"/api/accounts/{acct['id']}", json={"display_name": "x"}).status_code == 422


def test_remove_a_vpa_but_not_the_last4(asha):
    acct = asha.post("/api/accounts", json=BANK).json()
    vpa = asha.post(f"/api/accounts/{acct['id']}/identifiers", json={"kind": "vpa", "identifier": "a@ybl"}).json()
    setup = acct["identifiers"][0]
    assert asha.delete(f"/api/accounts/{acct['id']}/identifiers/{setup['id']}").status_code == 422
    assert asha.delete(f"/api/accounts/{acct['id']}/identifiers/{vpa['id']}").status_code == 204
    assert asha.get("/api/accounts/resolve", params={"identifier": "a@ybl"}).status_code == 404


def test_duplicate_vpa_is_409(asha):
    a = asha.post("/api/accounts", json=BANK).json()
    b = asha.post("/api/accounts", json=CARD).json()
    asha.post(f"/api/accounts/{a['id']}/identifiers", json={"kind": "vpa", "identifier": "a@ybl"})
    resp = asha.post(f"/api/accounts/{b['id']}/identifiers", json={"kind": "vpa", "identifier": "A@YBL"})
    assert resp.status_code == 409 and resp.json()["detail"] == "a@ybl is already on HDFC Savings."


def test_another_users_account_is_404_everywhere(app, asha):
    acct = asha.post("/api/accounts", json=BANK).json()
    asha.post(f"/api/accounts/{acct['id']}/identifiers", json={"kind": "vpa", "identifier": "asha@ybl"})
    make_user(app, "ravi", "ravi password 1")
    with TestClient(app) as ravi:
        login(ravi, "ravi", "ravi password 1")
        assert ravi.get("/api/accounts", params={"include_archived": "true"}).json() == []
        assert ravi.get(f"/api/accounts/{acct['id']}").status_code == 404
        assert ravi.patch(f"/api/accounts/{acct['id']}", json={"display_name": "mine"}).status_code == 404
        assert ravi.post(f"/api/accounts/{acct['id']}/archive").status_code == 404
        assert ravi.post(f"/api/accounts/{acct['id']}/identifiers",
                         json={"kind": "vpa", "identifier": "r@ybl"}).status_code == 404
        assert ravi.get("/api/accounts/resolve", params={"identifier": "asha@ybl"}).status_code == 404
    assert asha.get(f"/api/accounts/{acct['id']}").json()["display_name"] == "HDFC Savings"
