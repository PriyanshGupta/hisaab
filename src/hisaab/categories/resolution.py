"""Read-time category resolution (tech-design §6.5; ruling 2026-10-11).

A transaction's category is never stored. It is worked out on every read:

    override present  -> the override's category, or uncategorized if that
                         category is archived (never the rule: PRD §10.6)
    else rule present -> the payee rule's category, or uncategorized if archived
    else              -> uncategorized (NULL)

Both category joins filter archived_at IS NULL, so archiving a category makes
its transactions uncategorized and restoring brings them back, with no data
written either way. Every join also requires the same user_id, so a category
id belonging to someone else can never resolve, even if one were planted.

category_source says which mechanism applies ("override" / "rule" / None), so
a screen can say "uncategorized: its category is archived" rather than just "none".
Reports (slice 18) build on this same select.
"""

from sqlalchemy import Select, and_, case, literal, select
from sqlalchemy.orm import aliased

from hisaab.models import Category, Payee, PayeeCategory
from hisaab.models import TransactionEffective as T


def categorized(user_id: str) -> Select:
    """The user's effective transactions with category_id, category_source and payee_name."""
    override_cat = aliased(Category)
    rule_cat = aliased(Category)
    rule = aliased(PayeeCategory)
    has_override = T.category_override_id.is_not(None)
    return (
        select(
            T,
            case((has_override, override_cat.id), else_=rule_cat.id).label("category_id"),
            case((has_override, literal("override")), (rule.payee_id.is_not(None), literal("rule")),
                 else_=None).label("category_source"),
            Payee.normalized_name.label("payee_name"),
        )
        .outerjoin(override_cat, and_(override_cat.id == T.category_override_id, override_cat.user_id == T.user_id,
                                      override_cat.archived_at.is_(None)))
        .outerjoin(rule, and_(rule.user_id == T.user_id, rule.payee_id == T.payee_id))
        .outerjoin(rule_cat, and_(rule_cat.id == rule.category_id, rule_cat.user_id == T.user_id,
                                  rule_cat.archived_at.is_(None)))
        .outerjoin(Payee, Payee.id == T.payee_id)
        .where(T.user_id == user_id)
    )
