"""Categories and payee->category rules: the only writer of categories and
payee_categories. Both are per-user; every function filters on user_id.

- Each user starts with PRD §5.3's ten defaults (seed_defaults, called when the
  user is created; migration 0003 covers users who existed before).
- Categories are archived, never deleted. Archiving only sets archived_at:
  rules and overrides pointing at the category stay untouched, and read-time
  resolution (resolution.py) treats them as uncategorized until the category
  is restored. No data moves in either direction (tech-design §6.5).
- A rule maps one payee to one category for one user (Loop 2, PRD §5.1).
  Setting it again replaces it; setting None removes it.
- Names are unique per user ignoring case. An archived category keeps its name
  reserved, so re-creating it is answered with "restore it instead".
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from hisaab.models import Category, Payee, PayeeCategory

DEFAULT_CATEGORIES = (
    "Food & Dining", "Groceries", "Transport", "Bills & Utilities", "Rent",
    "Shopping", "Health", "Entertainment", "Lending", "Other",
)
MAX_NAME_LENGTH = 40


class CategoryError(ValueError):
    """A rule violation to show the person (message is user-facing)."""


class DuplicateCategory(CategoryError):
    pass


class NotFound(LookupError):
    pass


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def seed_defaults(session: Session, user_id: str) -> None:
    # One millisecond apart, ending now, so listing by created_at keeps the PRD's
    # order and anything the user creates afterwards lists after them.
    end = datetime.now(UTC)
    last = len(DEFAULT_CATEGORIES) - 1
    for i, name in enumerate(DEFAULT_CATEGORIES):
        session.add(Category(user_id=user_id, name=name, created_at=_iso(end - timedelta(milliseconds=last - i))))
    session.flush()


def list_categories(session: Session, user_id: str) -> list[Category]:
    """All the user's categories, archived included (callers filter for pickers)."""
    return list(session.scalars(
        select(Category).where(Category.user_id == user_id).order_by(Category.created_at, Category.id)
    ))


def get_category(session: Session, user_id: str, category_id: str) -> Category:
    category = session.scalars(
        select(Category).where(Category.id == category_id, Category.user_id == user_id)
    ).one_or_none()
    if category is None:
        raise NotFound("No such category")
    return category


def active_category(session: Session, user_id: str, category_id: str) -> Category:
    """A category the user may assign now: theirs and not archived."""
    category = get_category(session, user_id, category_id)
    if category.archived_at is not None:
        raise CategoryError(f"{category.name} is archived. Restore it first.")
    return category


def _check_name(session: Session, user_id: str, raw: str, exclude_id: str | None = None) -> str:
    name = " ".join((raw or "").split())
    if not name:
        raise CategoryError("Give the category a name.")
    if len(name) > MAX_NAME_LENGTH:
        raise CategoryError(f"Keep category names to {MAX_NAME_LENGTH} characters.")
    clash = session.scalars(
        select(Category).where(Category.user_id == user_id, func.lower(Category.name) == name.lower(),
                               Category.id != (exclude_id or ""))
    ).first()
    if clash is not None:
        if clash.archived_at is not None:
            raise DuplicateCategory(f"You have an archived category called {clash.name}. Restore it instead.")
        raise DuplicateCategory(f"You already have a category called {clash.name}.")
    return name


def create_category(session: Session, user_id: str, name: str) -> Category:
    category = Category(user_id=user_id, name=_check_name(session, user_id, name))
    session.add(category)
    session.flush()
    return category


def rename_category(session: Session, user_id: str, category_id: str, name: str) -> Category:
    category = get_category(session, user_id, category_id)
    category.name = _check_name(session, user_id, name, exclude_id=category.id)
    return category


def archive_category(session: Session, user_id: str, category_id: str) -> Category:
    category = get_category(session, user_id, category_id)
    if category.archived_at is None:
        category.archived_at = _iso(datetime.now(UTC))
    return category


def restore_category(session: Session, user_id: str, category_id: str) -> Category:
    category = get_category(session, user_id, category_id)
    category.archived_at = None
    return category


# --- rules -----------------------------------------------------------------------

def payee_rule(session: Session, user_id: str, payee_id: str) -> str | None:
    """The category id this user files the payee under, or None."""
    return session.scalar(
        select(PayeeCategory.category_id).where(PayeeCategory.user_id == user_id, PayeeCategory.payee_id == payee_id)
    )


def set_payee_category(session: Session, user_id: str, payee_id: str, category_id: str | None) -> None:
    """File every transaction of this payee under the category (None removes the rule).

    One row, not one edit per transaction: past and future transactions all
    follow it, and per-transaction overrides are left alone (PRD §3.11).
    """
    if session.get(Payee, payee_id) is None:
        raise NotFound("No such payee")
    if category_id is None:
        session.execute(delete(PayeeCategory).where(PayeeCategory.user_id == user_id,
                                                    PayeeCategory.payee_id == payee_id))
        return
    active_category(session, user_id, category_id)
    rule = session.get(PayeeCategory, (user_id, payee_id))
    if rule is None:
        session.add(PayeeCategory(user_id=user_id, payee_id=payee_id, category_id=category_id))
    else:
        rule.category_id = category_id
    session.flush()
