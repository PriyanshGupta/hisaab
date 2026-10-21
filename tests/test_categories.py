"""Categories (per user, archivable), the 10 defaults, and payee->category rules."""

from collections.abc import Callable
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, select, text
from sqlalchemy.orm import Session

from hisaab.auth import users
from hisaab.categories import repository as cats
from hisaab.categories.repository import CategoryError, DuplicateCategory, NotFound
from hisaab.config import Settings
from hisaab.db import Database
from hisaab.models import PayeeCategory
from tests import factories as f
from tests.conftest import alembic_config

DEFAULTS: list[str] = ["Food & Dining", "Groceries", "Transport", "Bills & Utilities", "Rent", "Shopping", "Health",
            "Entertainment", "Lending", "Other"]



@pytest.fixture
def asha(db: Database) -> str:
    with db.write_session() as s:
        return users.create_user(s, "asha", "Asha", "asha password 1", "member").id


def names(db: Database, user_id: str, include_archived: bool = False) -> list[str]:
    with db.read_session() as s:
        return [c.name for c in cats.list_categories(s, user_id) if include_archived or c.archived_at is None]


def cat_id(db: Database, user_id: str, name: str) -> str:
    with db.read_session() as s:
        return next(c.id for c in cats.list_categories(s, user_id) if c.name == name)


# --- defaults --------------------------------------------------------------------

def test_a_new_user_starts_with_the_ten_defaults(db: Database, asha: str) -> None:
    assert names(db, asha) == DEFAULTS


def test_each_user_gets_their_own_copies(db: Database, asha: str) -> None:
    with db.write_session() as s:
        ravi: str = users.create_user(s, "ravi", "Ravi", "ravi password 1", "member").id
    assert cat_id(db, asha, "Rent") != cat_id(db, ravi, "Rent")


def test_migration_0003_seeds_users_created_before_it(settings: Settings, migrated_engine: Engine) -> None:
    cfg: Config = alembic_config(settings.database_path)
    command.downgrade(cfg, "0002")
    with migrated_engine.begin() as conn:
        f.user(conn, id="early-user")
    command.upgrade(cfg, "head")
    with migrated_engine.connect() as conn:
        seeded: list[Any] = [r[0] for r in conn.execute(text("SELECT name FROM categories WHERE user_id='early-user'"))]
    assert sorted(seeded) == sorted(DEFAULTS)


def test_migration_0003_skips_users_who_already_have_categories(settings: Settings, migrated_engine: Engine) -> None:
    cfg: Config = alembic_config(settings.database_path)
    command.downgrade(cfg, "0002")
    with migrated_engine.begin() as conn:
        f.user(conn, id="has-one")
        f.category(conn, "has-one", name="Mine")
    command.upgrade(cfg, "head")
    with migrated_engine.connect() as conn:
        assert [r[0] for r in conn.execute(text("SELECT name FROM categories WHERE user_id='has-one'"))] == ["Mine"]


# --- CRUD ------------------------------------------------------------------------

def test_create_and_rename(db: Database, asha: str) -> None:
    with db.write_session() as s:
        gifts: str = cats.create_category(s, asha, "  Gifts ").id
    with db.write_session() as s:
        cats.rename_category(s, asha, gifts, "Gifts & Festivals")
    assert names(db, asha)[-1] == "Gifts & Festivals"


@pytest.mark.parametrize("name", ["", "   ", "x" * 41])
def test_bad_names_are_refused(db: Database, asha: str, name: str) -> None:
    with pytest.raises(CategoryError):
        with db.write_session() as s:
            cats.create_category(s, asha, name)


def test_names_are_unique_per_user_ignoring_case(db: Database, asha: str) -> None:
    with pytest.raises(DuplicateCategory, match="already have a category called Rent"):
        with db.write_session() as s:
            cats.create_category(s, asha, "rent")


def test_reusing_an_archived_name_says_to_restore_it(db: Database, asha: str) -> None:
    with db.write_session() as s:
        cats.archive_category(s, asha, cat_id(db, asha, "Lending"))
    with pytest.raises(DuplicateCategory, match="archived.*Restore it"):
        with db.write_session() as s:
            cats.create_category(s, asha, "Lending")


def test_archive_and_restore_only_flip_archived_at(db: Database, asha: str) -> None:
    rent: str = cat_id(db, asha, "Rent")
    with db.write_session() as s:
        cats.archive_category(s, asha, rent)
    assert "Rent" not in names(db, asha) and "Rent" in names(db, asha, include_archived=True)
    with db.write_session() as s:
        cats.restore_category(s, asha, rent)
    assert names(db, asha) == DEFAULTS
    assert cat_id(db, asha, "Rent") == rent  # the same row came back


def test_other_users_categories_are_invisible(db: Database, asha: str) -> None:
    with db.write_session() as s:
        ravi: str = users.create_user(s, "ravi", "Ravi", "ravi password 1", "member").id
    ashas_rent: str = cat_id(db, asha, "Rent")
    actions: list[Callable[[Session], object]] = [
        lambda s: cats.rename_category(s, ravi, ashas_rent, "Mine"),
        lambda s: cats.archive_category(s, ravi, ashas_rent),
        lambda s: cats.restore_category(s, ravi, ashas_rent),
    ]
    for action in actions:
        with pytest.raises(NotFound):
            with db.write_session() as s:
                action(s)


# --- rules -----------------------------------------------------------------------

def test_set_change_and_clear_a_payee_rule(db: Database, conn: Connection, asha: str) -> None:
    payee: str = f.payee(conn)["id"]
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


def test_a_rule_cannot_use_another_users_or_an_archived_category(db: Database, conn: Connection, asha: str) -> None:
    payee: str = f.payee(conn)["id"]
    conn.commit()
    with db.write_session() as s:
        ravi: str = users.create_user(s, "ravi", "Ravi", "ravi password 1", "member").id
    with pytest.raises(NotFound):
        with db.write_session() as s:
            cats.set_payee_category(s, ravi, payee, cat_id(db, asha, "Rent"))
    other: str = cat_id(db, asha, "Other")
    with db.write_session() as s:
        cats.archive_category(s, asha, other)
    with pytest.raises(CategoryError, match="archived"):
        with db.write_session() as s:
            cats.set_payee_category(s, asha, payee, other)


def test_a_rule_needs_a_real_payee(db: Database, asha: str) -> None:
    with pytest.raises(NotFound):
        with db.write_session() as s:
            cats.set_payee_category(s, asha, "no-such-payee", cat_id(db, asha, "Rent"))
