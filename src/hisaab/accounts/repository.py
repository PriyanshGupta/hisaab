"""Accounts repository: the only code that writes accounts and account_identifiers.

Every function takes the acting user's id and filters on it, so one family
member can never read, change or resolve another's accounts (tech-design §3.9).
A wrong user_id yields NotFound, never a permission error, so ids belonging to
someone else are not even confirmed to exist.

Rules owned here:
- Types: bank, credit_card, cash. Banks and cards need their last 4 digits
  (that is how alerts find them); cash has none. Only the last 4 digits are
  ever collected, never a full number (ruling 2026-10-11).
- Opening balance anchors (PRD §4.11), integer paise:
    bank  the balance on the anchor date (may be negative: an overdraft)
    card  the amount OWED on the anchor date, stored POSITIVE (PRD §10.3).
          A negative card opening is refused, because a debt typed as a minus
          number is the classic sign error that silently breaks every later
          reconciliation on the card.
    cash  cash in hand, never negative
- One active account per last4 per user, refused at creation with a message
  naming the holder: "You already have an account ending 5512: HDFC Savings."
- Accounts are archived, never deleted (PRD §6.6). Archiving retires the
  account's identifiers: they stop resolving at once, so a straggler alert for
  a closed account stays unresolved and reaches review instead of attaching
  somewhere silently (PRD §3.3), and they become reusable by a new account.
"""

import enum
import re
from collections.abc import Sequence
from datetime import date
from typing import Final

from sqlalchemy import Select, select, update
from sqlalchemy.orm import Session

from hisaab.accounts.identifiers import IdentifierError, normalize, normalize_for_lookup
from hisaab.clock import local_today
from hisaab.models import Account, AccountIdentifier, utc_now
from hisaab.models.core import ACCOUNT_TYPES

# The identifier each account type registers for itself at creation.
SETUP_IDENTIFIER_KIND: dict[str, str] = {"bank": "last4", "credit_card": "card_last4"}
# Kinds a user may add to an account afterwards.
USER_IDENTIFIER_KINDS: tuple[str, ...] = ("vpa", "other")

_ISO_DATE: re.Pattern[str] = re.compile(r"\d{4}-\d{2}-\d{2}")
# ₹10 lakh crore. Far beyond any household balance, inside SQLite's BIGINT and
# below JavaScript's exact-integer limit (~9e15), and the same cap money.ts uses.
MAX_ABS_PAISE: int = 10**15


class _Unset(enum.Enum):
    """An argument the caller left out (None is a real value, e.g. "no institution")."""

    UNSET = "unset"


_UNSET: Final = _Unset.UNSET


class AccountError(ValueError):
    """A rule violation to show the person (message is user-facing)."""


class DuplicateLast4(AccountError):
    pass


class DuplicateIdentifier(AccountError):
    pass


class NotFound(LookupError):
    pass


# --- validation --------------------------------------------------------------

def _check_type(account_type: str) -> None:
    if account_type not in ACCOUNT_TYPES:
        raise AccountError("Account type must be bank, credit_card or cash.")


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise AccountError("Give the account a name, e.g. 'HDFC Savings'.")
    return name


def _check_balance(account_type: str, paise: int) -> int:
    if not isinstance(paise, int) or isinstance(paise, bool):
        raise AccountError("Opening balance must be a whole number of paise.")
    if abs(paise) > MAX_ABS_PAISE:
        raise AccountError("That amount is too large.")
    if account_type == "credit_card" and paise < 0:
        raise AccountError(
            "A card's opening balance is the amount owed: enter it as a positive number "
            "(₹47,000 owed is 47000, not -47000)."
        )
    if account_type == "cash" and paise < 0:
        raise AccountError("Cash in hand can't be negative.")
    return paise


def _check_date(value: str) -> str:
    try:
        if not _ISO_DATE.fullmatch(value or ""):
            raise ValueError
        parsed: date = date.fromisoformat(value)
    except ValueError:
        raise AccountError("Opening balance date must be a real date (YYYY-MM-DD).") from None
    if parsed > local_today():  # India's date, not the (possibly UTC) server's
        raise AccountError("The opening balance date can't be in the future.")
    return value


def _check_anchor(account_type: str, balance: int | None, anchor: str | None) -> tuple[int | None, str | None]:
    """The opening balance is optional (ruling 2026-10-12), but a balance needs its date and a date its balance."""
    if balance is None and anchor is None:
        return None, None
    if balance is None or anchor is None:
        raise AccountError("Enter both the balance and its date, or neither.")
    return _check_balance(account_type, balance), _check_date(anchor)


def _check_last4(account_type: str, last4: str | None) -> str | None:
    if account_type == "cash":
        if last4:
            raise AccountError("Cash accounts don't have last 4 digits.")
        return None
    if not last4:
        raise AccountError("Bank accounts and cards need the last 4 digits of their number.")
    try:
        return normalize(SETUP_IDENTIFIER_KIND[account_type], last4)
    except IdentifierError as exc:
        raise AccountError(str(exc)) from None


# --- reads -------------------------------------------------------------------

def get_account(session: Session, user_id: str, account_id: str) -> Account:
    account: Account | None = session.scalars(
        select(Account).where(Account.id == account_id, Account.user_id == user_id)
    ).one_or_none()
    if account is None:
        raise NotFound("No such account")
    return account


def list_accounts(session: Session, user_id: str, include_archived: bool = False) -> list[Account]:
    """The user's accounts in creation order. Archived ones only when asked for:
    they must not appear in selection lists (PRD §6.6)."""
    query: Select[tuple[Account]] = select(Account).where(Account.user_id == user_id)
    if not include_archived:
        query = query.where(Account.archived_at.is_(None))
    return list(session.scalars(query.order_by(Account.created_at, Account.id)))


def active_identifiers(session: Session, user_id: str, account_id: str) -> list[AccountIdentifier]:
    return list(session.scalars(
        select(AccountIdentifier)
        .where(AccountIdentifier.user_id == user_id, AccountIdentifier.account_id == account_id,
               AccountIdentifier.retired_at.is_(None))
        .order_by(AccountIdentifier.created_at)
    ))


def resolve_identifier(session: Session, user_id: str, raw: str) -> Account | None:
    """The user's ACTIVE account this raw identifier belongs to, or None.

    None covers unknown, archived (retired) and ambiguous identifiers alike: the
    caller records the raw string as an unresolved leg and the transaction goes
    to review. It is never attached to a guessed or default account (PRD §3.3).
    """
    canonical: str | None = normalize_for_lookup(raw)
    if canonical is None:
        return None
    matches: Sequence[Account] = session.scalars(
        select(Account)
        .join(AccountIdentifier, AccountIdentifier.account_id == Account.id)
        .where(
            AccountIdentifier.user_id == user_id,
            Account.user_id == user_id,
            AccountIdentifier.identifier == canonical,
            AccountIdentifier.retired_at.is_(None),
            Account.archived_at.is_(None),
        )
    ).unique().all()
    # Two accounts claiming the same string (e.g. one's last4 equals another's
    # 'other' identifier) is ambiguous: refuse to pick.
    return matches[0] if len(matches) == 1 else None


# --- writes ------------------------------------------------------------------

def _active_account(session: Session, user_id: str, account_id: str) -> Account:
    account: Account = get_account(session, user_id, account_id)
    if account.archived_at is not None:
        raise AccountError("This account is archived and can't be changed.")
    return account


def create_account(
    session: Session,
    user_id: str,
    *,
    type: str,
    display_name: str,
    opening_balance: int | None = None,
    opening_balance_date: str | None = None,
    last4: str | None = None,
    institution: str | None = None,
) -> Account:
    _check_type(type)
    name: str = _check_name(display_name)
    canonical_last4: str | None = _check_last4(type, last4)
    balance, anchor = _check_anchor(type, opening_balance, opening_balance_date)

    if canonical_last4 is not None:
        holder: Account | None = session.scalars(
            select(Account).where(Account.user_id == user_id, Account.last4 == canonical_last4,
                                  Account.archived_at.is_(None))
        ).one_or_none()
        if holder is not None:
            raise DuplicateLast4(f"You already have an account ending {canonical_last4}: {holder.display_name}.")

    account: Account = Account(
        user_id=user_id, type=type, display_name=name, institution=(institution or "").strip() or None,
        last4=canonical_last4, opening_balance=balance, opening_balance_date=anchor,
    )
    session.add(account)
    session.flush()
    if canonical_last4 is not None:
        session.add(AccountIdentifier(
            account_id=account.id, user_id=user_id, kind=SETUP_IDENTIFIER_KIND[type],
            identifier=canonical_last4, origin="account_setup",
        ))
        session.flush()
    return account


def update_account(
    session: Session,
    user_id: str,
    account_id: str,
    *,
    display_name: str | _Unset = _UNSET,
    institution: str | None | _Unset = _UNSET,
    opening_balance: int | None | _Unset = _UNSET,
    opening_balance_date: str | None | _Unset = _UNSET,
) -> Account:
    """Rename, or correct the opening anchor. Type and last4 are fixed: a
    different card is a different account."""
    account: Account = _active_account(session, user_id, account_id)
    if display_name is not _UNSET:
        account.display_name = _check_name(display_name)
    if institution is not _UNSET:
        account.institution = (institution or "").strip() or None
    if opening_balance is not _UNSET or opening_balance_date is not _UNSET:  # checked as a pair: both or neither
        account.opening_balance, account.opening_balance_date = _check_anchor(
            account.type, account.opening_balance if isinstance(opening_balance, _Unset) else opening_balance,
            account.opening_balance_date if isinstance(opening_balance_date, _Unset) else opening_balance_date)
    return account


def archive_account(session: Session, user_id: str, account_id: str) -> Account:
    """Archive (idempotent). The row and its transactions stay; identifiers retire."""
    account: Account = get_account(session, user_id, account_id)
    if account.archived_at is None:
        now: str = utc_now()
        account.archived_at = now
        session.execute(
            update(AccountIdentifier)
            .where(AccountIdentifier.account_id == account.id, AccountIdentifier.user_id == user_id,
                   AccountIdentifier.retired_at.is_(None))
            .values(retired_at=now)
        )
    return account


def add_identifier(session: Session, user_id: str, account_id: str, kind: str, raw: str) -> AccountIdentifier:
    account: Account = _active_account(session, user_id, account_id)
    if kind not in USER_IDENTIFIER_KINDS:
        raise AccountError("Add UPI IDs or other references here; last 4 digits are set when the account is created.")
    try:
        canonical: str = normalize(kind, raw)
    except IdentifierError as exc:
        raise AccountError(str(exc)) from None
    holder: Account | None = session.scalars(
        select(Account)
        .join(AccountIdentifier, AccountIdentifier.account_id == Account.id)
        .where(AccountIdentifier.user_id == user_id, AccountIdentifier.kind == kind,
               AccountIdentifier.identifier == canonical, AccountIdentifier.retired_at.is_(None))
    ).first()
    if holder is not None:
        raise DuplicateIdentifier(f"{canonical} is already on {holder.display_name}.")
    identifier: AccountIdentifier = AccountIdentifier(account_id=account.id, user_id=user_id, kind=kind,
                                   identifier=canonical, origin="account_setup")
    session.add(identifier)
    session.flush()
    return identifier


def assign_identifier(session: Session, user_id: str, account_id: str, raw: str) -> tuple[AccountIdentifier, str]:
    """"Ending 4821 is my HDFC savings" (PRD §3.3): register what an alert printed on one of the user's
    accounts, from review. Returns the row and the canonical form; the caller resolves past transactions.

    Refused, naming the holder, if another active account already claims that string: identifiers are
    matched exactly and a wrong account is worse than none. Already on this account: returned as is.
    """
    account: Account = _active_account(session, user_id, account_id)
    canonical: str | None = normalize_for_lookup(raw)
    if canonical is None:
        raise AccountError("That doesn't look like an account number, card number or UPI ID.")
    existing: AccountIdentifier | None = session.scalars(select(AccountIdentifier).where(
        AccountIdentifier.user_id == user_id, AccountIdentifier.identifier == canonical,
        AccountIdentifier.retired_at.is_(None))).first()
    if existing is not None:
        if existing.account_id != account.id:
            holder: Account = get_account(session, user_id, existing.account_id)
            raise DuplicateIdentifier(f"{canonical} is already on {holder.display_name}.")
        return existing, canonical
    kind: str = ("vpa" if "@" in canonical else
                 SETUP_IDENTIFIER_KIND.get(account.type, "last4") if canonical.isdigit() and len(canonical) == 4 else "other")
    identifier: AccountIdentifier = AccountIdentifier(account_id=account.id, user_id=user_id, kind=kind,
                                                      identifier=canonical, origin="review_assignment")
    session.add(identifier)
    session.flush()
    return identifier, canonical


def removable(identifier: AccountIdentifier) -> bool:
    """Everything but the last 4 digits the account was created with: one assigned from review by mistake can go.
    (Transactions it already resolved stay on the account until that period is re-synced.)"""
    return not (identifier.origin == "account_setup" and identifier.kind in SETUP_IDENTIFIER_KIND.values())


def remove_identifier(session: Session, user_id: str, account_id: str, identifier_id: str) -> None:
    _active_account(session, user_id, account_id)
    identifier: AccountIdentifier | None = session.scalars(
        select(AccountIdentifier).where(
            AccountIdentifier.id == identifier_id, AccountIdentifier.account_id == account_id,
            AccountIdentifier.user_id == user_id, AccountIdentifier.retired_at.is_(None),
        )
    ).one_or_none()
    if identifier is None:
        raise NotFound("No such identifier")
    if not removable(identifier):
        raise AccountError("The last 4 digits identify this account and can't be removed. Archive the account instead.")
    session.delete(identifier)
