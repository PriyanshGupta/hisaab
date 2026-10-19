"""Accounts repository: create, edit, archive, identifiers, resolution. All user-scoped."""

from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import Connection, select

from hisaab.accounts import repository as accounts
from hisaab.accounts.repository import AccountError, DuplicateIdentifier, DuplicateLast4, NotFound
from hisaab.clock import local_today
from hisaab.db import Database
from hisaab.models import Account, AccountIdentifier
from tests import factories as f

ANCHOR: str = "2026-07-11"



@pytest.fixture
def asha(conn: Connection) -> str:
    uid: str = f.user(conn)["id"]
    conn.commit()
    return uid


@pytest.fixture
def ravi(conn: Connection) -> str:
    uid: str = f.user(conn)["id"]
    conn.commit()
    return uid


def create(db: Database, user_id: str, **kw: Any) -> str:
    args: dict[str, Any] = {"type": "bank", "display_name": "HDFC Savings", "institution": "HDFC Bank", "last4": "5512",
            "opening_balance": 8420000, "opening_balance_date": ANCHOR} | kw
    with db.write_session() as s:
        return accounts.create_account(s, user_id, **args).id


def resolve(db: Database, user_id: str, raw: str) -> str | None:
    with db.read_session() as s:
        acct: Account | None = accounts.resolve_identifier(s, user_id, raw)
        return acct.id if acct else None


# --- create ----------------------------------------------------------------

def test_create_all_three_types(db: Database, asha: str) -> None:
    bank: str = create(db, asha)
    card: str = create(db, asha, type="credit_card", display_name="Regalia card", last4="8830", opening_balance=4700000)
    cash: str = create(db, asha, type="cash", display_name="Wallet", institution=None, last4=None, opening_balance=350000)
    with db.read_session() as s:
        got: dict[str, tuple[str, str | None, int | None]] = {a.id: (a.type, a.last4, a.opening_balance) for a in accounts.list_accounts(s, asha)}
    assert got == {bank: ("bank", "5512", 8420000), card: ("credit_card", "8830", 4700000), cash: ("cash", None, 350000)}


def test_card_opening_balance_is_stored_positive_as_debt(db: Database, asha: str) -> None:
    card: str = create(db, asha, type="credit_card", display_name="Card", last4="8830", opening_balance=4700000)
    with db.read_session() as s:
        assert accounts.get_account(s, asha, card).opening_balance == 4700000  # owed ₹47,000 -> +4,700,000 paise


def test_negative_card_opening_balance_is_refused(db: Database, asha: str) -> None:
    with pytest.raises(AccountError, match="amount owed"):
        create(db, asha, type="credit_card", display_name="Card", last4="8830", opening_balance=-4700000)


def test_negative_cash_is_refused(db: Database, asha: str) -> None:
    with pytest.raises(AccountError, match="Cash"):
        create(db, asha, type="cash", display_name="Wallet", last4=None, opening_balance=-1)


def test_bank_may_be_overdrawn(db: Database, asha: str) -> None:
    create(db, asha, opening_balance=-150000)


@pytest.mark.parametrize("kw,message", [
    ({"last4": None}, "last 4 digits"),
    ({"type": "credit_card", "last4": None}, "last 4 digits"),
    ({"type": "cash", "last4": "1234"}, "Cash accounts don't have"),
    ({"last4": "12"}, "4 digits"),
    ({"display_name": "   "}, "name"),
    ({"type": "wallet"}, "type"),
    ({"opening_balance_date": "2026-13-01"}, "date"),
    ({"opening_balance_date": (local_today() + timedelta(days=1)).isoformat()}, "future"),  # India's tomorrow
])
def test_create_validation(db: Database, asha: str, kw: dict[str, Any], message: str) -> None:
    with pytest.raises(AccountError, match=message):
        create(db, asha, **kw)


def test_duplicate_active_last4_is_refused_with_the_holders_name(db: Database, asha: str) -> None:
    create(db, asha, display_name="HDFC Savings", last4="5512")
    with pytest.raises(DuplicateLast4, match="You already have an account ending 5512: HDFC Savings"):
        create(db, asha, type="credit_card", display_name="Some card", last4="5512")


def test_same_last4_for_different_users_is_fine(db: Database, asha: str, ravi: str) -> None:
    create(db, asha, last4="5512")
    create(db, ravi, last4="5512")


def test_create_registers_the_last4_identifier(db: Database, asha: str) -> None:
    bank: str = create(db, asha, last4="XX5512")
    card: str = create(db, asha, type="credit_card", display_name="Card", last4="8830", opening_balance=0)
    with db.read_session() as s:
        rows: set[tuple[str, str, str, str]] = {(i.account_id, i.kind, i.identifier, i.origin) for i in s.scalars(select(AccountIdentifier))}
    assert rows == {(bank, "last4", "5512", "account_setup"), (card, "card_last4", "8830", "account_setup")}


# --- identifiers and resolution -------------------------------------------------

def test_vpa_lookup_is_case_insensitive(db: Database, asha: str) -> None:
    bank: str = create(db, asha)
    with db.write_session() as s:
        accounts.add_identifier(s, asha, bank, "vpa", "upi@ybl")
    assert resolve(db, asha, "UPI@YBL") == bank
    assert resolve(db, asha, "  Upi@Ybl ") == bank


def test_vpa_stored_in_capitals_is_found_in_lower_case(db: Database, asha: str) -> None:
    bank: str = create(db, asha)
    with db.write_session() as s:
        accounts.add_identifier(s, asha, bank, "vpa", "UPI@YBL")
    assert resolve(db, asha, "upi@ybl") == bank


def test_last4_resolves_from_masked_forms(db: Database, asha: str) -> None:
    card: str = create(db, asha, type="credit_card", display_name="Card", last4="8830", opening_balance=0)
    assert resolve(db, asha, "XX8830") == card
    assert resolve(db, asha, "**** **** **** 8830") == card


def test_unknown_identifier_resolves_to_none(db: Database, asha: str) -> None:
    create(db, asha)
    assert resolve(db, asha, "someone@ybl") is None
    assert resolve(db, asha, "1111") is None


def test_duplicate_active_identifier_is_refused_with_the_holders_name(db: Database, asha: str) -> None:
    a: str = create(db, asha, display_name="HDFC Savings", last4="5512")
    b: str = create(db, asha, display_name="ICICI Savings", last4="2218")
    with db.write_session() as s:
        accounts.add_identifier(s, asha, a, "vpa", "asha@ybl")
    with pytest.raises(DuplicateIdentifier, match="asha@ybl is already on HDFC Savings"):
        with db.write_session() as s:
            accounts.add_identifier(s, asha, b, "vpa", "ASHA@YBL")


def test_setup_identifier_cannot_be_removed(db: Database, asha: str) -> None:
    bank: str = create(db, asha)
    with db.read_session() as s:
        setup_id: str = s.scalars(select(AccountIdentifier.id)).one()
    with pytest.raises(AccountError, match="last 4 digits"):
        with db.write_session() as s:
            accounts.remove_identifier(s, asha, bank, setup_id)


def test_removed_vpa_stops_resolving(db: Database, asha: str) -> None:
    bank: str = create(db, asha)
    with db.write_session() as s:
        ident: str = accounts.add_identifier(s, asha, bank, "vpa", "asha@ybl").id
    with db.write_session() as s:
        accounts.remove_identifier(s, asha, bank, ident)
    assert resolve(db, asha, "asha@ybl") is None


# --- archive ---------------------------------------------------------------

def test_archive_hides_account_and_stops_its_identifiers_resolving(db: Database, asha: str) -> None:
    bank: str = create(db, asha)
    with db.write_session() as s:
        accounts.add_identifier(s, asha, bank, "vpa", "upi@ybl")
    with db.write_session() as s:
        accounts.archive_account(s, asha, bank)
    with db.read_session() as s:
        assert [a.id for a in accounts.list_accounts(s, asha)] == []
        assert [a.id for a in accounts.list_accounts(s, asha, include_archived=True)] == [bank]
        assert accounts.get_account(s, asha, bank).archived_at is not None  # retained, never deleted
    assert resolve(db, asha, "UPI@YBL") is None  # a straggler stays unresolved -> review queue
    assert resolve(db, asha, "5512") is None


def test_archived_identifiers_can_be_reused_by_a_new_account(db: Database, asha: str) -> None:
    old: str = create(db, asha, display_name="Old HDFC", last4="5512")
    with db.write_session() as s:
        accounts.add_identifier(s, asha, old, "vpa", "asha@ybl")
    with db.write_session() as s:
        accounts.archive_account(s, asha, old)
    new: str = create(db, asha, display_name="New HDFC", last4="5512")
    with db.write_session() as s:
        accounts.add_identifier(s, asha, new, "vpa", "asha@ybl")
    assert resolve(db, asha, "asha@ybl") == new
    assert resolve(db, asha, "5512") == new


def test_archived_account_is_read_only(db: Database, asha: str) -> None:
    bank: str = create(db, asha)
    with db.write_session() as s:
        accounts.archive_account(s, asha, bank)
    with pytest.raises(AccountError, match="archived"):
        with db.write_session() as s:
            accounts.update_account(s, asha, bank, display_name="Renamed")
    with pytest.raises(AccountError, match="archived"):
        with db.write_session() as s:
            accounts.add_identifier(s, asha, bank, "vpa", "x@ybl")


# --- edit ------------------------------------------------------------------

def test_rename_and_correct_the_anchor(db: Database, asha: str) -> None:
    bank: str = create(db, asha)
    with db.write_session() as s:
        accounts.update_account(s, asha, bank, display_name="HDFC Salary", opening_balance=9000000,
                                opening_balance_date="2026-07-01")
    with db.read_session() as s:
        a: Account = accounts.get_account(s, asha, bank)
        assert (a.display_name, a.opening_balance, a.opening_balance_date) == ("HDFC Salary", 9000000, "2026-07-01")


def test_edit_applies_the_same_balance_rules(db: Database, asha: str) -> None:
    card: str = create(db, asha, type="credit_card", display_name="Card", last4="8830", opening_balance=0)
    with pytest.raises(AccountError, match="amount owed"):
        with db.write_session() as s:
            accounts.update_account(s, asha, card, opening_balance=-100)


# --- user scoping -----------------------------------------------------------

def test_other_users_accounts_are_invisible(db: Database, asha: str, ravi: str) -> None:
    bank: str = create(db, asha)
    with db.write_session() as s:
        accounts.add_identifier(s, asha, bank, "vpa", "asha@ybl")
    with db.read_session() as s:
        assert accounts.list_accounts(s, ravi, include_archived=True) == []
        with pytest.raises(NotFound):
            accounts.get_account(s, ravi, bank)
    assert resolve(db, ravi, "asha@ybl") is None
    assert resolve(db, ravi, "5512") is None
    for action in (
        lambda s: accounts.update_account(s, ravi, bank, display_name="Mine now"),
        lambda s: accounts.archive_account(s, ravi, bank),
        lambda s: accounts.add_identifier(s, ravi, bank, "vpa", "ravi@ybl"),
    ):
        with pytest.raises(NotFound):
            with db.write_session() as s:
                action(s)
