"""The reconciliation check's lifecycle (tech-design §8.4; rulings 2026-10-20), and manual checkpoints.

A gap is live: an observation against the anchor before it plus the movements between (ledger.checks),
computed from today's data. balance_deltas rows carry what happened to it:

    open          record_checks found a gap beyond tolerance with no open row for that observation
    investigated  the cause was fixed elsewhere: allowed only once the live gap is back within tolerance,
                  so closing can never hide a gap that still exists; nothing is written
    accepted      an `adjustment` transaction moves the balance by exactly the gap, dated the observation's
                  day ("dated to the period it arose in"); visible in the list, outside every total

A new observation is its own interval, checked from the anchor before it, so it never absorbs an open
gap. A gap that comes back after closing (a later edit) is recorded again as a new open row.

The check runs before every commit that changed a user's transactions, corrections, accounts or balance
observations (one SQLAlchemy hook, below), so a gap an edit creates is recorded wherever the edit came
from: the transaction list, a review answer, a sync, a statement (review finding, 2026-10-20).
"""

import hashlib
import json
from datetime import date, datetime, time
from itertools import chain
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from hisaab.balances import BALANCE_USERS, mark_changed

from hisaab.balances.ledger import Check, checks, local_date
from hisaab.clock import LOCAL_TZ, local_today
from hisaab.models import (
    Account,
    BalanceDelta,
    BalanceObservation,
    Source,
    Transaction,
    TransactionEdit,
    TransactionSource,
    utc_now,
)
from hisaab.transactions.legs import validate_legs
from hisaab.transactions.repository import derived_key, next_ingest_seq
from hisaab.tunables import tunable

ADJUSTMENT_PAYEE: str = "Balance adjustment"
MAX_BALANCE: int = 10**15  # paise, as accounts.repository's opening balance bound


class CheckError(ValueError):
    """The action can't be done (message is a sentence for the person)."""


class NoSuch(CheckError):
    """The balance or account isn't the user's, or doesn't exist (404, never "forbidden")."""


def tolerance(session: Session, user_id: str) -> int:
    value: int = tunable(session, "balance_delta_tolerance_paise", user_id)
    return value


def _active_accounts(session: Session, user_id: str) -> list[Account]:
    return list(session.scalars(select(Account).where(Account.user_id == user_id, Account.archived_at.is_(None))))


def _open_row(session: Session, user_id: str, observation_id: str) -> BalanceDelta | None:
    return session.scalars(select(BalanceDelta).where(
        BalanceDelta.user_id == user_id, BalanceDelta.observation_id == observation_id,
        BalanceDelta.status == "open")).first()


def _row(user_id: str, account_id: str, check: Check, status: str) -> BalanceDelta:
    return BalanceDelta(user_id=user_id, account_id=account_id, computed_balance=check.expected,
                        observed_balance=check.observed, delta=check.gap, arose_from=check.since.on,
                        observation_id=check.observation_id, status=status)


def record_checks(session: Session, user_id: str) -> int:
    """Open a row for every live gap beyond tolerance that has none. Idempotent; returns rows created."""
    session.flush()
    limit: int = tolerance(session, user_id)
    created: int = 0
    for account in _active_accounts(session, user_id):
        for check in checks(session, user_id, account):
            if abs(check.gap) > limit and _open_row(session, user_id, check.observation_id) is None:
                session.add(_row(user_id, account.id, check, "open"))
                created += 1
    session.flush()
    return created


def _find(session: Session, user_id: str, observation_id: str) -> tuple[Account, Check | None]:
    obs: BalanceObservation | None = session.get(BalanceObservation, observation_id)
    if obs is None or obs.user_id != user_id:
        raise NoSuch("No such balance.")
    account: Account = session.get_one(Account, obs.account_id)
    return account, next((c for c in checks(session, user_id, account) if c.observation_id == observation_id), None)


def accept(session: Session, user_id: str, observation_id: str) -> BalanceDelta:
    session.flush()
    account, check = _find(session, user_id, observation_id)
    if check is None or abs(check.gap) <= tolerance(session, user_id):
        raise CheckError("This balance already matches: there's no gap to accept.")
    out_of: bool = (check.gap < 0) != (account.type == "credit_card")  # bank less / card owes more: money out
    validate_legs("adjustment", account.type if out_of else None, None if out_of else account.type)
    made: Transaction = _adjustment(session, user_id, account, abs(check.gap), check.observed_on, out_of)
    row: BalanceDelta = _open_row(session, user_id, observation_id) or _row(user_id, account.id, check, "open")
    row.computed_balance, row.observed_balance, row.delta = check.expected, check.observed, check.gap
    row.status, row.adjustment_transaction_id, row.resolved_at = "accepted", made.id, utc_now()
    session.add(row)
    session.flush()
    return row


def _adjustment(session: Session, user_id: str, account: Account, amount: int, on: str, out_of: bool) -> Transaction:
    """A manual Source like a typed entry, so provenance reads "manual" and review never asks about it."""
    content: bytes = json.dumps({"kind": "adjustment", "account_id": account.id, "amount": amount, "on": on,
                                 "out_of": out_of}, sort_keys=True).encode()
    now: str = utc_now()
    seq: int = next_ingest_seq(session)
    source: Source = Source(ingest_seq=seq, user_id=user_id, kind="manual", content=content,
                            content_hash=hashlib.sha256(content).hexdigest(), received_at=now, ingested_at=now,
                            processing_state="parsed")
    session.add(source)
    session.flush()
    txn: Transaction = Transaction(
        user_id=user_id, derived_key=derived_key(user_id, seq, "0"), amount=amount, kind="adjustment",
        from_account_id=account.id if out_of else None, to_account_id=None if out_of else account.id,
        payee_raw=ADJUSTMENT_PAYEE, transaction_date=on, status="confirmed", primary_source_id=source.id)
    session.add(txn)
    session.flush()
    session.add(TransactionSource(transaction_id=txn.id, source_id=source.id))
    session.flush()
    return txn


def close(session: Session, user_id: str, observation_id: str) -> BalanceDelta:
    session.flush()
    _, check = _find(session, user_id, observation_id)
    row: BalanceDelta | None = _open_row(session, user_id, observation_id)
    if row is None:
        raise CheckError("There's no open gap on this balance to close.")
    if check is not None and abs(check.gap) > tolerance(session, user_id):
        raise CheckError("The gap is still there, so it can't be closed as investigated. "
                         "Fix the cause or accept it as an adjustment.")
    if check is not None:
        row.computed_balance, row.observed_balance, row.delta = check.expected, check.observed, check.gap
    row.status, row.resolved_at = "investigated", utc_now()
    session.flush()
    return row


def add_checkpoint(session: Session, user_id: str, account_id: str, balance: int, on: str) -> BalanceObservation:
    """The manual checkpoint (PRD §4.11): the balance at the end of `on` (now, when `on` is today)."""
    account: Account | None = session.get(Account, account_id)
    if account is None or account.user_id != user_id or account.archived_at is not None:
        raise NoSuch("No such account.")
    try:
        day: date = date.fromisoformat(on)
    except ValueError:
        raise CheckError("The date must be a real date (YYYY-MM-DD).") from None
    today: date = local_today()
    if day > today:
        raise CheckError("A balance can't be in the future.")
    if type(balance) is not int or abs(balance) >= MAX_BALANCE:
        raise CheckError("The balance must be a whole number of paise.")
    if account.type == "credit_card" and balance < 0:
        raise CheckError("For a card, enter what you owe as a positive amount.")
    observed_at: str = utc_now() if day == today else utc_now(datetime.combine(day, time(23, 59, 59), LOCAL_TZ))
    same_day: BalanceObservation | None = next(
        (o for o in session.scalars(select(BalanceObservation).where(
            BalanceObservation.user_id == user_id, BalanceObservation.account_id == account.id,
            BalanceObservation.source == "manual")) if local_date(o.observed_at) == day.isoformat()), None)
    if same_day is not None:  # a correction (a typo): replace in place, as a re-uploaded statement does
        same_day.balance, same_day.observed_at, same_day.created_at = balance, observed_at, utc_now()
        obs: BalanceObservation = same_day
    else:
        obs = BalanceObservation(user_id=user_id, account_id=account.id, balance=balance, observed_at=observed_at,
                                 source="manual")
        session.add(obs)
    session.flush()
    record_checks(session, user_id)
    return obs


# --- the commit hook ---------------------------------------------------------------------------------------

_CHECKING: str = "checking_balances"


@event.listens_for(Session, "after_flush")
def _note(session: Session, _context: Any) -> None:
    if session.info.get(_CHECKING):
        return
    for obj in chain(session.new, session.dirty, session.deleted):
        if isinstance(obj, (Transaction, TransactionEdit, BalanceObservation, Account)):
            mark_changed(session, obj.user_id)


@event.listens_for(Session, "before_commit")
def _check_before_commit(session: Session) -> None:
    if session.info.get(_CHECKING):
        return
    session.flush()  # pending changes reach _note first
    users: set[str] = session.info.pop(BALANCE_USERS, set())
    if not users:
        return
    session.info[_CHECKING] = True
    try:
        for user_id in sorted(users):
            record_checks(session, user_id)
        session.flush()
    finally:
        session.info.pop(_CHECKING, None)
