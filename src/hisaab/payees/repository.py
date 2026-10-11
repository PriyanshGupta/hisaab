"""Payees repository: the only writer of payees.

Payees are global (PRD §5.2): "Swiggy" is the same merchant for everyone, so a
rename shows for every family member (accepted, tech-design §11.5). Names are
unique ignoring case, so "swiggy" typed on the Add screen links to the existing
"Swiggy" rather than creating a second one. Which category a payee falls under
is per-user and lives in categories.repository (payee_categories).
"""

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from hisaab.models import Payee
from hisaab.models.core import PAYEE_TYPES

MAX_NAME_LENGTH: int = 80


class PayeeError(ValueError):
    """A rule violation to show the person (message is user-facing)."""


class DuplicatePayee(PayeeError):
    pass


class NotFound(LookupError):
    pass


def _clean(raw: str) -> str:
    name: str = " ".join((raw or "").split())
    if not name:
        raise PayeeError("Give the payee a name.")
    if len(name) > MAX_NAME_LENGTH:
        raise PayeeError(f"Keep payee names to {MAX_NAME_LENGTH} characters.")
    return name


def _by_name(session: Session, name: str) -> Payee | None:
    return session.scalars(select(Payee).where(func.lower(Payee.normalized_name) == name.lower())).first()


def _check_type(payee_type: str) -> str:
    if payee_type not in PAYEE_TYPES:
        raise PayeeError("A payee is a merchant or a person.")
    return payee_type


def get_payee(session: Session, payee_id: str) -> Payee:
    payee: Payee | None = session.get(Payee, payee_id)
    if payee is None:
        raise NotFound("No such payee")
    return payee


def list_payees(session: Session, query: str | None = None, limit: int = 500) -> list[Payee]:
    stmt: Select[tuple[Payee]] = select(Payee).order_by(func.lower(Payee.normalized_name)).limit(limit)
    if query and query.strip():
        stmt = stmt.where(func.lower(Payee.normalized_name).contains(query.strip().lower()))
    return list(session.scalars(stmt))


def create_payee(session: Session, name: str, payee_type: str = "merchant") -> Payee:
    name = _clean(name)
    _check_type(payee_type)
    if (existing := _by_name(session, name)) is not None:
        raise DuplicatePayee(f"{existing.normalized_name} already exists.")
    payee: Payee = Payee(normalized_name=name, type=payee_type)
    session.add(payee)
    session.flush()
    return payee


def find_by_name(session: Session, name: str) -> Payee | None:
    """The payee with this name, ignoring case and spacing, or None."""
    return _by_name(session, _clean(name))


def get_or_create_by_name(session: Session, name: str) -> Payee:
    """The payee with this name (ignoring case and spacing), created as a merchant if new."""
    name = _clean(name)
    return _by_name(session, name) or create_payee(session, name)


def update_payee(session: Session, payee_id: str, *, name: str | None = None, type: str | None = None) -> Payee:
    payee: Payee = get_payee(session, payee_id)
    if name is not None:
        name = _clean(name)
        clash: Payee | None = _by_name(session, name)
        if clash is not None and clash.id != payee.id:
            raise DuplicatePayee(f"{clash.normalized_name} already exists.")
        payee.normalized_name = name
    if type is not None:
        payee.type = _check_type(type)
    return payee
