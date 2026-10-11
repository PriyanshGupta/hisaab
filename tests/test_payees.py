"""Payees: global (shared by every user), names unique ignoring case."""

import pytest

from hisaab.db import Database
from hisaab.payees import repository as payees
from hisaab.payees.repository import DuplicatePayee, NotFound, PayeeError


@pytest.fixture
def db(migrated_engine):
    return Database(migrated_engine)


def create(db, name, type="merchant"):
    with db.write_session() as s:
        return payees.create_payee(s, name, type).id


def test_create_list_and_search(db):
    create(db, "Swiggy")
    create(db, "Swiggy Instamart")
    create(db, "Rajesh Kumar", "person")
    with db.read_session() as s:
        assert [p.normalized_name for p in payees.list_payees(s)] == ["Rajesh Kumar", "Swiggy", "Swiggy Instamart"]
        assert [p.normalized_name for p in payees.list_payees(s, query="swig")] == ["Swiggy", "Swiggy Instamart"]


def test_names_are_unique_ignoring_case(db):
    create(db, "Swiggy")
    with pytest.raises(DuplicatePayee, match="Swiggy already exists"):
        create(db, "  swiggy ")


@pytest.mark.parametrize("name,type", [("", "merchant"), ("x" * 81, "merchant"), ("Ok", "robot")])
def test_bad_payees_are_refused(db, name, type):
    with pytest.raises(PayeeError):
        create(db, name, type)


def test_get_or_create_links_to_an_existing_payee_regardless_of_case(db):
    swiggy = create(db, "Swiggy")
    with db.write_session() as s:
        assert payees.get_or_create_by_name(s, "SWIGGY").id == swiggy
        tea = payees.get_or_create_by_name(s, " Tea   stall ")
        assert tea.normalized_name == "Tea stall" and tea.type == "merchant"
    with db.read_session() as s:
        assert len(payees.list_payees(s)) == 2


def test_rename_and_retype(db):
    pid = create(db, "Rajesh")
    with db.write_session() as s:
        payees.update_payee(s, pid, name="Rajesh Kumar", type="person")
    with db.read_session() as s:
        p = payees.get_payee(s, pid)
        assert (p.normalized_name, p.type) == ("Rajesh Kumar", "person")


def test_rename_cannot_take_another_payees_name(db):
    create(db, "Swiggy")
    zomato = create(db, "Zomato")
    with pytest.raises(DuplicatePayee):
        with db.write_session() as s:
            payees.update_payee(s, zomato, name="swiggy")


def test_unknown_payee(db):
    with pytest.raises(NotFound):
        with db.read_session() as s:
            payees.get_payee(s, "nope")
