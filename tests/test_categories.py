"""Categories (per user, archivable), the 10 defaults, and payee->category rules."""

import pytest
from alembic import command
from sqlalchemy import select, text

from hisaab.auth import users
from hisaab.categories import repository as cats
from hisaab.categories.repository import CategoryError, DuplicateCategory, NotFound
from hisaab.db import Database
from hisaab.models import Category, PayeeCategory
from tests import factories as f
from tests.conftest import alembic_config

DEFAULTS = ["Food & Dining", "Groceries", "Transport", "Bills & Utilities", "Rent", "Shopping", "Health",
            "Entertainment", "Lending", "Other"]


@pytest.fixture
def db(migrated_engine):
    return Database(migrated_engine)


@pytest.fixture
def asha(db):
    with db.write_session() as s:
        return users.create_user(s, "asha", "Asha", "asha password 1", "member").id


def names(db, user_id, include_archived=False):
    with db.read_session() as s:
        return [c.name for c in cats.list_categories(s, user_id) if include_archived or c.archived_at is None]


def cat_id(db, user_id, name):
    with db.read_session() as s:
        return next(c.id for c in cats.list_categories(s, user_id) if c.name == name)


# --- defaults --------------------------------------------------------------------

def test_a_new_user_starts_with_the_ten_defaults(db, asha):
    assert names(db, asha) == DEFAULTS


def test_each_user_gets_their_own_copies(db, asha):
    with db.write_session() as s:
        ravi = users.create_user(s, "ravi", "Ravi", "ravi password 1", "member").id
    assert cat_id(db, asha, "Rent") != cat_id(db, ravi, "Rent")


def test_migration_0003_seeds_users_created_before_it(settings, migrated_engine):
    cfg = alembic_config(settings.database_path)
    command.downgrade(cfg, "0002")
    with migrated_engine.begin() as conn:
        f.user(conn, id="early-user")
    command.upgrade(cfg, "head")
    with migrated_engine.connect() as conn:
        seeded = [r[0] for r in conn.execute(text("SELECT name FROM categories WHERE user_id='early-user'"))]
    assert sorted(seeded) == sorted(DEFAULTS)


def test_migration_0003_skips_users_who_already_have_categories(settings, migrated_engine):
    cfg = alembic_config(settings.database_path)
    command.downgrade(cfg, "0002")
    with migrated_engine.begin() as conn:
        f.user(conn, id="has-one")
        f.category(conn, "has-one", name="Mine")
    command.upgrade(cfg, "head")
    with migrated_engine.connect() as conn:
        assert [r[0] for r in conn.execute(text("SELECT name FROM categories WHERE user_id='has-one'"))] == ["Mine"]


# --- CRUD ------------------------------------------------------------------------

def test_create_and_rename(db, asha):
    with db.write_session() as s:
        gifts = cats.create_category(s, asha, "  Gifts ").id
    with db.write_session() as s:
        cats.rename_category(s, asha, gifts, "Gifts & Festivals")
    assert names(db, asha)[-1] == "Gifts & Festivals"


@pytest.mark.parametrize("name", ["", "   ", "x" * 41])
def test_bad_names_are_refused(db, asha, name):
    with pytest.raises(CategoryError):
        with db.write_session() as s:
            cats.create_category(s, asha, name)


def test_names_are_unique_per_user_ignoring_case(db, asha):
    with pytest.raises(DuplicateCategory, match="already have a category called Rent"):
        with db.write_session() as s:
            cats.create_category(s, asha, "rent")


def test_reusing_an_archived_name_says_to_restore_it(db, asha):
    with db.write_session() as s:
        cats.archive_category(s, asha, cat_id(db, asha, "Lending"))
    with pytest.raises(DuplicateCategory, match="archived.*Restore it"):
        with db.write_session() as s:
            cats.create_category(s, asha, "Lending")


def test_archive_and_restore_only_flip_archived_at(db, asha):
    rent = cat_id(db, asha, "Rent")
    with db.write_session() as s:
        cats.archive_category(s, asha, rent)
    assert "Rent" not in names(db, asha) and "Rent" in names(db, asha, include_archived=True)
    with db.write_session() as s:
        cats.restore_category(s, asha, rent)
    assert names(db, asha) == DEFAULTS
    assert cat_id(db, asha, "Rent") == rent  # the same row came back


def test_other_users_categories_are_invisible(db, asha):
    with db.write_session() as s:
        ravi = users.create_user(s, "ravi", "Ravi", "ravi password 1", "member").id
    ashas_rent = cat_id(db, asha, "Rent")
    for action in (lambda s: cats.rename_category(s, ravi, ashas_rent, "Mine"),
                   lambda s: cats.archive_category(s, ravi, ashas_rent),
                   lambda s: cats.restore_category(s, ravi, ashas_rent)):
        with pytest.raises(NotFound):
            with db.write_session() as s:
                action(s)


# --- rules -----------------------------------------------------------------------

def test_set_change_and_clear_a_payee_rule(db, conn, asha):
    payee = f.payee(conn)["id"]
    conn.commit()
    food, groceries = cat_id(db, asha, "Food & Dining"), cat_id(db, asha, "Groceries")
    with db.write_session() as s:
        cats.set_payee_category(s, asha, payee, food)
    with db.write_session() as s:
        cats.set_payee_category(s, asha, payee, groceries)  # one rule per (user, payee): replaced
    with db.read_session() as s:
        assert s.scalars(select(PayeeCategory.category_id).where(PayeeCategory.user_id == asha)).all() == [groceries]
        assert cats.payee_rule(s, asha, payee) == groceries
    with db.write_session() as s:
        cats.set_payee_category(s, asha, payee, None)
    with db.read_session() as s:
        assert cats.payee_rule(s, asha, payee) is None


def test_a_rule_cannot_use_another_users_or_an_archived_category(db, conn, asha):
    payee = f.payee(conn)["id"]
    conn.commit()
    with db.write_session() as s:
        ravi = users.create_user(s, "ravi", "Ravi", "ravi password 1", "member").id
    with pytest.raises(NotFound):
        with db.write_session() as s:
            cats.set_payee_category(s, ravi, payee, cat_id(db, asha, "Rent"))
    other = cat_id(db, asha, "Other")
    with db.write_session() as s:
        cats.archive_category(s, asha, other)
    with pytest.raises(CategoryError, match="archived"):
        with db.write_session() as s:
            cats.set_payee_category(s, asha, payee, other)


def test_a_rule_needs_a_real_payee(db, asha):
    with pytest.raises(NotFound):
        with db.write_session() as s:
            cats.set_payee_category(s, asha, "no-such-payee", cat_id(db, asha, "Rent"))
