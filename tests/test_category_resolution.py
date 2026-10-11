"""Read-time category resolution (tech-design §6.5, ruling 2026-10-11):
override if present (uncategorized if its category is archived), else the payee
rule (if its category is active), else uncategorized. Nothing is ever written."""

import pytest
from sqlalchemy import func, select

from hisaab.accounts import repository as accounts
from hisaab.auth import users
from hisaab.categories import repository as cats
from hisaab.categories.resolution import categorized
from hisaab.db import Database
from hisaab.models import Category, PayeeCategory, TransactionEdit
from hisaab.transactions import repository as txns
from hisaab.transactions.repository import TransactionError
from tests import factories as f


@pytest.fixture
def db(migrated_engine):
    return Database(migrated_engine)


@pytest.fixture
def asha(db):
    with db.write_session() as s:
        uid = users.create_user(s, "asha", "Asha", "asha password 1", "member").id
        accounts.create_account(s, uid, type="cash", display_name="Wallet", opening_balance=0,
                                opening_balance_date="2026-07-01")
    return uid


def wallet(db, user_id):
    with db.read_session() as s:
        return accounts.list_accounts(s, user_id)[0].id


def cat(db, user_id, name):
    with db.read_session() as s:
        return next(c.id for c in cats.list_categories(s, user_id) if c.name == name)


def enter(db, user_id, payee, amount=10000, category_id=None):
    with db.write_session() as s:
        return txns.create_manual(s, user_id, kind="expense", amount=amount, transaction_date="2026-10-05",
                                  from_account_id=wallet(db, user_id), payee_raw=payee, category_id=category_id).id


def resolved(db, user_id):
    """{transaction id: (category id, source)} as read time sees it."""
    with db.read_session() as s:
        return {r.TransactionEffective.id: (r.category_id, r.category_source) for r in s.execute(categorized(user_id))}


def payee_of(db, txn_id):
    with db.read_session() as s:
        return s.execute(select(f.T["transactions"].c.payee_id).where(f.T["transactions"].c.id == txn_id)).scalar()


def set_rule(db, user_id, payee_id, category_id):
    with db.write_session() as s:
        cats.set_payee_category(s, user_id, payee_id, category_id)


def override(db, user_id, txn_id, category_id):
    with db.write_session() as s:
        txns.apply_edits(s, user_id, txn_id, {"category_override_id": category_id})


def archive(db, user_id, category_id, restore=False):
    with db.write_session() as s:
        (cats.restore_category if restore else cats.archive_category)(s, user_id, category_id)


def row_counts(db):
    with db.read_session() as s:
        return tuple(s.scalar(select(func.count()).select_from(m)) for m in (PayeeCategory, TransactionEdit, Category))


# --- the basic cases -------------------------------------------------------------------

def test_without_rule_or_override_a_transaction_is_uncategorized(db, asha):
    t = enter(db, asha, "Tea stall")
    assert resolved(db, asha) == {t: (None, None)}


def test_a_payee_rule_categorizes_all_that_payees_transactions(db, asha):
    teas = [enter(db, asha, "Tea stall") for _ in range(3)]
    other = enter(db, asha, "Auto rickshaw")
    food = cat(db, asha, "Food & Dining")
    set_rule(db, asha, payee_of(db, teas[0]), food)
    got = resolved(db, asha)
    assert all(got[t] == (food, "rule") for t in teas)
    assert got[other] == (None, None)


def test_an_override_survives_a_rule_change(db, asha):
    a, b = enter(db, asha, "Swiggy"), enter(db, asha, "Swiggy")
    food, groceries, gifts = cat(db, asha, "Food & Dining"), cat(db, asha, "Groceries"), cat(db, asha, "Other")
    set_rule(db, asha, payee_of(db, a), food)
    override(db, asha, b, gifts)
    set_rule(db, asha, payee_of(db, a), groceries)  # the rule changes
    got = resolved(db, asha)
    assert got[a] == (groceries, "rule")
    assert got[b] == (gifts, "override")  # untouched by the rule change
    with db.write_session() as s:
        txns.revert(s, asha, b, ["category_override_id"])
    assert resolved(db, asha)[b] == (groceries, "rule")  # undo -> follows the new rule


# --- archive / restore: no data moves ------------------------------------------------------

def test_archiving_uncategorizes_and_restoring_brings_back_with_no_data_change(db, asha):
    a, b = enter(db, asha, "Landlord"), enter(db, asha, "Landlord")
    rent, other = cat(db, asha, "Rent"), cat(db, asha, "Other")
    set_rule(db, asha, payee_of(db, a), rent)
    override(db, asha, b, other)
    before = row_counts(db)

    archive(db, asha, rent)
    assert resolved(db, asha) == {a: (None, "rule"), b: (other, "override")}
    assert row_counts(db) == before  # nothing written but archived_at

    archive(db, asha, rent, restore=True)
    assert resolved(db, asha) == {a: (rent, "rule"), b: (other, "override")}
    assert row_counts(db) == before


def test_an_override_to_an_archived_category_is_uncategorized_not_the_rule(db, asha):
    t = enter(db, asha, "Swiggy")
    food, gifts = cat(db, asha, "Food & Dining"), cat(db, asha, "Other")
    set_rule(db, asha, payee_of(db, t), food)
    override(db, asha, t, gifts)
    archive(db, asha, gifts)
    assert resolved(db, asha)[t] == (None, "override")  # not food
    archive(db, asha, gifts, restore=True)
    assert resolved(db, asha)[t] == (gifts, "override")


def test_another_users_category_never_resolves_even_if_planted(db, conn, asha):
    t = enter(db, asha, "Swiggy")
    ravi = f.user(conn)["id"]
    ravis = f.category(conn, ravi)["id"]
    f.transaction_edit(conn, t, asha, field="category_override_id", new_value=ravis)
    conn.commit()
    assert resolved(db, asha)[t] == (None, "override")


# --- payee and category corrections ---------------------------------------------------------

def test_correcting_the_payee_follows_the_new_payees_rule(db, asha):
    t = enter(db, asha, "Swigy")  # a typo
    right = enter(db, asha, "Swiggy")
    food = cat(db, asha, "Food & Dining")
    set_rule(db, asha, payee_of(db, right), food)
    with db.write_session() as s:
        txns.apply_edits(s, asha, t, {"payee_id": payee_of(db, right)})
    assert resolved(db, asha)[t] == (food, "rule")


@pytest.mark.parametrize("field,value,message", [
    ("payee_id", "no-such-payee", "No such payee"),
    ("category_override_id", "no-such-category", "No such category"),
])
def test_corrections_must_point_at_real_things(db, asha, field, value, message):
    t = enter(db, asha, "Swiggy")
    with pytest.raises(TransactionError, match=message):
        with db.write_session() as s:
            txns.apply_edits(s, asha, t, {field: value})


def test_cannot_override_to_an_archived_or_someone_elses_category(db, conn, asha):
    t = enter(db, asha, "Swiggy")
    other = cat(db, asha, "Other")
    archive(db, asha, other)
    with pytest.raises(TransactionError, match="archived"):
        override(db, asha, t, other)
    with db.write_session() as s:
        ravi = users.create_user(s, "ravi", "Ravi", "ravi password 1", "member").id
    with pytest.raises(TransactionError, match="No such category"):
        override(db, asha, t, cat(db, ravi, "Rent"))


# --- manual entry: the category pick (ruling: learn, then override) ----------------------------

def test_manual_entry_links_the_typed_payee_case_insensitively(db, asha):
    a, b = enter(db, asha, "Tea stall"), enter(db, asha, "TEA STALL")
    assert payee_of(db, a) == payee_of(db, b) is not None


def test_first_pick_for_a_payee_becomes_its_rule(db, asha):
    food = cat(db, asha, "Food & Dining")
    first = enter(db, asha, "Tea stall", category_id=food)
    later = enter(db, asha, "Tea stall")  # no pick: the learned rule applies
    with db.read_session() as s:
        assert cats.payee_rule(s, asha, payee_of(db, first)) == food
    assert resolved(db, asha) == {first: (food, "rule"), later: (food, "rule")}


def test_a_different_pick_overrides_just_that_entry(db, asha):
    food, gifts = cat(db, asha, "Food & Dining"), cat(db, asha, "Other")
    usual = enter(db, asha, "Tea stall", category_id=food)
    special = enter(db, asha, "Tea stall", category_id=gifts)
    with db.read_session() as s:
        assert cats.payee_rule(s, asha, payee_of(db, usual)) == food  # rule unchanged
    assert resolved(db, asha) == {usual: (food, "rule"), special: (gifts, "override")}


def test_picking_the_rules_own_category_writes_nothing_extra(db, asha):
    food = cat(db, asha, "Food & Dining")
    enter(db, asha, "Tea stall", category_id=food)
    enter(db, asha, "Tea stall", category_id=food)
    assert row_counts(db)[:2] == (1, 0)  # one rule, no overrides


def test_a_pick_without_a_payee_is_an_override(db, asha):
    food = cat(db, asha, "Food & Dining")
    t = enter(db, asha, None, category_id=food)
    assert resolved(db, asha)[t] == (food, "override")
