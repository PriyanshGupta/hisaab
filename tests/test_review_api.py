"""/api/review (slice 17; PRD §5.5, §3.11, §3.3): the queue, and answers that resolve many at once.

- A category for a payee is ONE rule; overrides are kept and counted.
- An account for an identifier is ONE registry row; every past and future
  transaction carrying it resolves. "Just this one" is an edit.
- A name for a raw payee string: an admin writes one global pattern; a member
  writes edits on their own matching rows (ruling 2026-10-12).
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Connection, func, select

from hisaab.db import Database
from hisaab.models import NormalizationPattern, PayeeCategory, Transaction, TransactionSource
from hisaab.payees import repository as payees
from tests import factories as f
from tests.conftest import login, make_user
from tests.gmail_fake import make_eml
from tests.pipeline_helpers import BANK_EMAIL, contract, ingest, make_accounts

RAW: str = "UPI/DR/111111/TEST PAYEE@okexample"


@pytest.fixture
def admin(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        make_user(app, "asha", "asha password 1", role="admin")
        login(c, "asha", "asha password 1")
        yield c


def uid(client: TestClient) -> str:
    value: str = client.get("/api/auth/me").json()["id"]
    return value


def db_of(app: FastAPI) -> Database:
    db: Database = app.state.db
    return db


def corner_shop(app: FastAPI) -> str:
    with db_of(app).write_session() as s:
        payee_id: str = payees.create_payee(s, "Corner Shop").id
        s.add(NormalizationPattern(match_type="exact", pattern="Corner Shop", payee_id=payee_id, origin="seed"))
        return payee_id


def category(app: FastAPI, user_id: str, name: str) -> str:
    with db_of(app).write_session() as s:
        cat_id: str = f.category(s.connection(), user_id, name=name)["id"]
        return cat_id


def txn_ids(app: FastAPI, source_ids: list[str]) -> list[str]:
    with db_of(app).read_session() as s:
        return [s.scalars(select(TransactionSource.transaction_id).where(TransactionSource.source_id == i)).one()
                for i in source_ids]


def base(app: FastAPI, txn_id: str) -> Transaction:
    with db_of(app).read_session() as s:
        t: Transaction = s.get_one(Transaction, txn_id)
        s.expunge(t)
        return t


# --- a category for a payee: one rule ------------------------------------------------------------

def test_categorizing_23_of_one_payee_reports_23_updated_and_the_overrides_kept(app: FastAPI, admin: TestClient) -> None:
    me: str = uid(admin)
    make_accounts(db_of(app), me)
    payee: str = corner_shop(app)
    _, sources = ingest(db_of(app), me, *[(contract(amount=10_000 + i), BANK_EMAIL) for i in range(23)])
    ids: list[str] = txn_ids(app, sources)
    gifts, food = category(app, me, "Gifts"), category(app, me, "Food")
    admin.patch(f"/api/transactions/{ids[0]}", json={"category_override_id": gifts})
    resp: Response = admin.post("/api/review/payee-category", json={"payee_id": payee, "category_id": food})
    assert resp.json() == {"updated": 22, "overrides_kept": 1}
    with db_of(app).read_session() as s:
        rules: list[PayeeCategory] = list(s.scalars(select(PayeeCategory)))
        assert [(r.payee_id, r.category_id, r.origin, r.batch_id) for r in rules] == [(payee, food, "user", None)]
    assert admin.get(f"/api/transactions/{ids[0]}").json()["category_id"] == gifts  # the override survives
    assert admin.get(f"/api/transactions/{ids[1]}").json()["category_id"] == food


def test_an_override_on_an_archived_category_is_still_counted_as_kept(app: FastAPI, admin: TestClient) -> None:
    me: str = uid(admin)
    make_accounts(db_of(app), me)
    payee: str = corner_shop(app)
    _, sources = ingest(db_of(app), me, (contract(), BANK_EMAIL), (contract(amount=7), BANK_EMAIL))
    ids: list[str] = txn_ids(app, sources)
    old: str = category(app, me, "Old")
    admin.patch(f"/api/transactions/{ids[0]}", json={"category_override_id": old})
    admin.post(f"/api/categories/{old}/archive")
    resp: Response = admin.post("/api/review/payee-category", json={"payee_id": payee,
                                                                    "category_id": category(app, me, "Food")})
    assert resp.json() == {"updated": 1, "overrides_kept": 1}
    assert admin.get(f"/api/transactions/{ids[0]}").json()["category_id"] is None  # archived override: uncategorized


# --- an account for an identifier: one registry row ----------------------------------------------

def test_assigning_an_identifier_resolves_every_past_transaction_and_the_next_sync(app: FastAPI,
                                                                                 admin: TestClient) -> None:
    me: str = uid(admin)
    accounts: dict[str, str] = make_accounts(db_of(app), me)
    corner_shop(app)
    _, sources = ingest(db_of(app), me, *[(contract(account_identifier="XX4821", amount=100 + i), BANK_EMAIL)
                                          for i in range(3)])
    resp: Response = admin.post("/api/review/identifier", json={"identifier": "XX4821", "account_id": accounts["bank"]})
    assert resp.json() == {"resolved": 3}
    for t in map(lambda i: base(app, i), txn_ids(app, sources)):
        assert (t.from_account_id, t.unresolved_from_identifier, t.kind, t.status) == \
            (accounts["bank"], None, "expense", "confirmed")
    _, [later] = ingest(db_of(app), me, (contract(account_identifier="4821", amount=999), BANK_EMAIL))
    assert base(app, txn_ids(app, [later])[0]).from_account_id == accounts["bank"]  # resolved at parse


def test_setting_one_transactions_account_writes_an_edit_and_leaves_the_rest(app: FastAPI, admin: TestClient) -> None:
    me: str = uid(admin)
    accounts: dict[str, str] = make_accounts(db_of(app), me)
    _, sources = ingest(db_of(app), me, (contract(account_identifier="4821"), BANK_EMAIL),
                        (contract(account_identifier="4821", amount=2), BANK_EMAIL))
    one, other = txn_ids(app, sources)
    admin.patch(f"/api/transactions/{one}", json={"from_account_id": accounts["other_bank"]})
    assert admin.get(f"/api/transactions/{one}").json()["from_account_id"] == accounts["other_bank"]
    assert base(app, one).unresolved_from_identifier == "4821"  # the source still says so; the edit overlays it
    assert base(app, other).unresolved_from_identifier == "4821"


def test_an_identifier_another_account_holds_is_refused_naming_it(app: FastAPI, admin: TestClient) -> None:
    accounts: dict[str, str] = make_accounts(db_of(app), uid(admin))
    resp: Response = admin.post("/api/review/identifier", json={"identifier": "XX2222", "account_id": accounts["bank"]})
    assert resp.status_code == 409 and resp.json()["detail"] == "2222 is already on other_bank."


def test_an_identifier_assigned_by_mistake_can_be_removed(app: FastAPI, admin: TestClient) -> None:
    accounts: dict[str, str] = make_accounts(db_of(app), uid(admin))
    admin.post("/api/review/identifier", json={"identifier": "4821", "account_id": accounts["bank"]})
    ids: list[dict[str, Any]] = admin.get(f"/api/accounts/{accounts['bank']}").json()["identifiers"]
    assigned: dict[str, Any] = next(i for i in ids if i["identifier"] == "4821")
    setup_last4: dict[str, Any] = next(i for i in ids if i["identifier"] == "1111")
    assert (assigned["removable"], setup_last4["removable"]) == (True, False)
    assert admin.delete(f"/api/accounts/{accounts['bank']}/identifiers/{assigned['id']}").status_code == 204


def test_adding_a_upi_id_on_the_accounts_page_resolves_past_rows(app: FastAPI, admin: TestClient) -> None:
    me: str = uid(admin)
    accounts: dict[str, str] = make_accounts(db_of(app), me)
    _, [src] = ingest(db_of(app), me, (contract(account_identifier="asha@okhdfc"), BANK_EMAIL))
    admin.post(f"/api/accounts/{accounts['bank']}/identifiers", json={"kind": "vpa", "identifier": "Asha@OKHDFC"})
    assert base(app, txn_ids(app, [src])[0]).from_account_id == accounts["bank"]


def test_not_my_account_keeps_them_as_expenses_and_stops_asking(app: FastAPI, admin: TestClient) -> None:
    """A FASTag wallet pays tolls: those are expenses, the wallet isn't an account to track (user, 2026-10-19)."""
    me: str = uid(admin)
    make_accounts(db_of(app), me)
    corner_shop(app)
    _, sources = ingest(db_of(app), me, (contract(account_identifier="90000012345678", amount=11100), BANK_EMAIL),
                        (contract(account_identifier="FASTag Wallet 90000012345678", amount=22200), BANK_EMAIL))
    groups: set[str | None] = {i["identifier"] for i in admin.get("/api/review").json()["items"]}
    assert groups == {"5678"}  # one wallet printed two ways is one group
    assert admin.post("/api/review/not-my-account", json={"identifier": "5678"}).json() == {"quieted": 2}
    for t in map(lambda i: base(app, i), txn_ids(app, sources)):
        assert (t.kind, t.status, t.from_account_id) == ("expense", "confirmed", None)
    _, [later] = ingest(db_of(app), me, (contract(account_identifier="90000012345678", amount=999), BANK_EMAIL))
    assert base(app, txn_ids(app, [later])[0]).status == "confirmed"  # the next toll doesn't ask either
    assert all("account" not in i["tags"] for i in admin.get("/api/review").json()["items"])


def test_not_my_account_is_per_user(app: FastAPI, admin: TestClient) -> None:
    with db_of(app).write_session() as s:
        other: str = f.user(s.connection())["id"]
    corner_shop(app)
    _, [theirs] = ingest(db_of(app), other, (contract(account_identifier="5678"), BANK_EMAIL))
    admin.post("/api/review/not-my-account", json={"identifier": "5678"})
    assert base(app, txn_ids(app, [theirs])[0]).review_reason == "account 5678 isn't one of yours yet"


def test_creating_an_account_resolves_past_rows_for_its_last4(app: FastAPI, admin: TestClient) -> None:
    me: str = uid(admin)
    _, [src] = ingest(db_of(app), me, (contract(account_identifier="XX4821"), BANK_EMAIL))
    created: str = admin.post("/api/accounts", json={"type": "bank", "display_name": "HDFC", "last4": "4821"}).json()["id"]
    assert base(app, txn_ids(app, [src])[0]).from_account_id == created


# --- a name for a raw payee string ---------------------------------------------------------------

def test_an_admin_naming_writes_one_pattern_and_resolves_every_matching_row(app: FastAPI, admin: TestClient) -> None:
    me: str = uid(admin)
    make_accounts(db_of(app), me)
    _, sources = ingest(db_of(app), me, (contract(payee_raw=RAW), BANK_EMAIL), (contract(payee_raw=RAW, amount=5), BANK_EMAIL))
    resp: Response = admin.post("/api/review/name-payee", json={"payee_raw": RAW, "name": "Test Payee"})
    body: dict[str, Any] = resp.json()
    assert (body["updated"], body["pattern"]) == (2, True)
    assert {base(app, t).payee_id for t in txn_ids(app, sources)} == {body["payee_id"]}
    with db_of(app).read_session() as s:
        assert s.scalar(select(func.count()).select_from(NormalizationPattern)) == 1
    _, [later] = ingest(db_of(app), me, (contract(payee_raw=RAW, amount=9), BANK_EMAIL))
    assert base(app, txn_ids(app, [later])[0]).payee_id == body["payee_id"]  # future rows resolve at parse


def test_a_member_naming_writes_edits_on_their_rows_only(app: FastAPI, admin: TestClient) -> None:
    with TestClient(app) as ravi:
        make_user(app, "ravi", "ravi password 1")
        login(ravi, "ravi", "ravi password 1")
        ravi_id: str = uid(ravi)
        make_accounts(db_of(app), ravi_id)
        _, mine = ingest(db_of(app), ravi_id, (contract(payee_raw=RAW), BANK_EMAIL))
        _, theirs = ingest(db_of(app), uid(admin), (contract(payee_raw=RAW, account_identifier="7777"), BANK_EMAIL))
        body: dict[str, Any] = ravi.post("/api/review/name-payee", json={"payee_raw": RAW, "name": "Test Payee"}).json()
        assert (body["updated"], body["pattern"]) == (1, False)
        assert ravi.get(f"/api/transactions/{txn_ids(app, mine)[0]}").json()["payee_id"] == body["payee_id"]
        assert base(app, txn_ids(app, theirs)[0]).payee_id is None
        with db_of(app).read_session() as s:
            assert s.scalar(select(func.count()).select_from(NormalizationPattern)) == 0


def test_an_unknown_payee_group_is_named_on_just_those_transactions(app: FastAPI, admin: TestClient) -> None:
    me: str = uid(admin)
    make_accounts(db_of(app), me)
    _, sources = ingest(db_of(app), me, (contract(payee_raw=None), BANK_EMAIL), (contract(payee_raw=None, amount=4), BANK_EMAIL))
    ids: list[str] = txn_ids(app, sources)
    body: dict[str, Any] = admin.post("/api/review/name-payee", json={"transaction_ids": ids, "name": "Milkman"}).json()
    assert (body["updated"], body["pattern"]) == (2, False)  # no text to match on: edits, never a pattern
    assert {admin.get(f"/api/transactions/{i}").json()["payee_name"] for i in ids} == {"Milkman"}


def test_an_unknown_account_group_is_given_an_account_by_edit(app: FastAPI, admin: TestClient) -> None:
    me: str = uid(admin)
    accounts: dict[str, str] = make_accounts(db_of(app), me)
    corner_shop(app)
    _, sources = ingest(db_of(app), me, (contract(account_identifier=None), BANK_EMAIL),
                        (contract(account_identifier=None, amount=4), BANK_EMAIL))
    ids: list[str] = txn_ids(app, sources)
    resp: Response = admin.post("/api/review/assign-account", json={"transaction_ids": ids,
                                                                    "account_id": accounts["bank"]})
    assert resp.json() == {"updated": 2}
    assert {admin.get(f"/api/transactions/{i}").json()["from_account_id"] for i in ids} == {accounts["bank"]}


def test_someone_elses_transaction_ids_are_refused(app: FastAPI, admin: TestClient) -> None:
    with db_of(app).write_session() as s:
        other: str = f.user(s.connection())["id"]
    _, [theirs] = ingest(db_of(app), other, (contract(payee_raw=None), BANK_EMAIL))
    resp: Response = admin.post("/api/review/name-payee", json={"transaction_ids": txn_ids(app, [theirs]), "name": "X"})
    assert resp.status_code == 404


# --- the queue -----------------------------------------------------------------------------------

def test_the_queue_lists_needs_review_and_uncategorized_with_tags(app: FastAPI, admin: TestClient) -> None:
    me: str = uid(admin)
    make_accounts(db_of(app), me)
    corner_shop(app)
    ingest(db_of(app), me, (contract(account_identifier="XX4821"), BANK_EMAIL),  # account + uncategorized
           (contract(payee_raw=RAW, amount=2), BANK_EMAIL),                     # payee
           (contract(amount=3), BANK_EMAIL))                                    # confirmed, uncategorized
    items: list[dict[str, Any]] = admin.get("/api/review").json()["items"]
    by_amount: dict[int, dict[str, Any]] = {i["amount_paise"]: i for i in items}
    assert by_amount[50000]["tags"] == ["account", "uncategorized"] and by_amount[50000]["identifier"] == "4821"
    assert (by_amount[50000]["account_label"], by_amount[50000]["account_leg"]) == ("ending 4821", "from")
    assert by_amount[2]["tags"] == ["payee", "uncategorized"] and by_amount[2]["payee_raw"] == RAW
    assert by_amount[3]["tags"] == ["uncategorized"] and by_amount[3]["payee_name"] == "Corner Shop"
    assert by_amount[3]["account_label"] == "bank"


def test_categorized_payees_stay_in_the_queue_answer_as_sorted_rows(app: FastAPI, admin: TestClient) -> None:
    """User, 2026-10-19: a categorized payee moves to the bottom, still visible and changeable, never vanishes."""
    me: str = uid(admin)
    make_accounts(db_of(app), me)
    payee: str = corner_shop(app)
    ingest(db_of(app), me, (contract(), BANK_EMAIL), (contract(amount=7), BANK_EMAIL))
    food: str = category(app, me, "Food")
    admin.post("/api/review/payee-category", json={"payee_id": payee, "category_id": food})
    body: dict[str, Any] = admin.get("/api/review").json()
    assert body["items"] == []  # nothing left to answer
    [row] = body["categorized"]
    assert (row["payee_id"], row["payee_name"], row["category_id"], row["origin"]) == (payee, "Corner Shop", food, "user")
    assert sorted(i["amount_paise"] for i in row["items"]) == [7, 50000]


def test_sorted_accounts_are_there_after_a_reload(app: FastAPI, admin: TestClient) -> None:
    """User, 2026-10-19: "ending 6203" assigned to an account must still show under Sorted after a reload."""
    me: str = uid(admin)
    accounts: dict[str, str] = make_accounts(db_of(app), me)
    corner_shop(app)
    ingest(db_of(app), me, (contract(account_identifier="XX4821"), BANK_EMAIL), (contract(amount=7), BANK_EMAIL),
           (contract(account_identifier="90000012345678", amount=11100), BANK_EMAIL))
    admin.post("/api/review/identifier", json={"identifier": "4821", "account_id": accounts["bank"]})
    admin.post("/api/review/not-my-account", json={"identifier": "5678"})
    rows: list[dict[str, Any]] = admin.get("/api/review").json()["sorted_accounts"]
    bank: dict[str, Any] = next(r for r in rows if r["account_id"] == accounts["bank"])
    assert (bank["label"], bank["answer"], len(bank["items"])) == ("Ending 1111, ending 4821", "bank", 2)
    wallet: dict[str, Any] = next(r for r in rows if r["account_id"] is None)
    assert (wallet["label"], wallet["answer"], len(wallet["items"])) == ("Ending 5678", None, 1)


def test_sorted_payees_are_there_after_a_reload(app: FastAPI, admin: TestClient) -> None:
    me: str = uid(admin)
    make_accounts(db_of(app), me)
    _, sources = ingest(db_of(app), me, (contract(payee_raw=RAW), BANK_EMAIL), (contract(payee_raw=RAW, amount=5), BANK_EMAIL),
                        (contract(payee_raw=None, amount=9), BANK_EMAIL))
    named: str = admin.post("/api/review/name-payee", json={"payee_raw": RAW, "name": "Test Payee"}).json()["payee_id"]
    admin.post("/api/review/name-payee", json={"transaction_ids": txn_ids(app, sources[2:]), "name": "Milkman"})
    rows: list[dict[str, Any]] = admin.get("/api/review").json()["sorted_payees"]
    by_label: dict[str, dict[str, Any]] = {r["label"]: r for r in rows}
    assert (by_label[RAW]["payee_name"], by_label[RAW]["payee_id"], len(by_label[RAW]["items"])) == ("Test Payee", named, 2)
    assert (by_label["Unknown payee"]["payee_name"], len(by_label["Unknown payee"]["items"])) == ("Milkman", 1)


def test_an_item_carries_its_emails_details_and_the_kept_text_can_be_read(app: FastAPI, admin: TestClient) -> None:
    me: str = uid(admin)
    eml: bytes = make_eml("alerts@bank.example", "Debit alert", "<p>Rs 500 debited from a/c ending 1111</p>")
    with db_of(app).write_session() as s:
        c: Connection = s.connection()
        f.gmail_connection(c, user_id=me, email_address="asha@example.test")
        src: str = f.source(c, me, content=eml, external_message_id="18f2c0ab", received_at="2026-10-05T03:44:00.000Z",
                            diagnostic=json.dumps({"sender": "alerts@bank.example", "review": ["couldn't read: date"]}))["id"]
        txn: str = f.transaction(c, me, source_id=src, status="needs_review", review_reason="couldn't read: date")["id"]
    item: dict[str, Any] = next(i for i in admin.get("/api/review").json()["items"] if i["id"] == txn)
    assert item["email"] == {"received_at": "2026-10-05T03:44:00.000Z", "sender": "alerts@bank.example",
                             "subject": "Debit alert", "has_text": True,
                             "gmail_url": "https://mail.google.com/mail/?authuser=asha%40example.test#all/18f2c0ab"}
    assert "Rs 500 debited" in admin.get(f"/api/review/email/{txn}").json()["text"]


def test_an_emails_text_that_was_not_kept_says_so_and_is_private(app: FastAPI, admin: TestClient) -> None:
    me: str = uid(admin)
    with db_of(app).write_session() as s:
        c: Connection = s.connection()
        mine: str = f.transaction(c, me, primary_source_id=f.source(c, me, content=None)["id"])["id"]
        other: str = f.user(c)["id"]
        theirs: str = f.transaction(c, other)["id"]
    assert admin.get(f"/api/review/email/{mine}").status_code == 404
    assert admin.get(f"/api/review/email/{theirs}").status_code == 404


def test_another_users_rows_never_appear(app: FastAPI, admin: TestClient) -> None:
    with db_of(app).write_session() as s:
        other: str = f.user(s.connection())["id"]
    ingest(db_of(app), other, (contract(), BANK_EMAIL))
    assert admin.get("/api/review").json() == {"items": [], "categorized": [], "sorted_accounts": [],
                                               "sorted_payees": []}
