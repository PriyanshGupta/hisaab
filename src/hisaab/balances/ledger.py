"""What each account's balance should be, from the app's own data (reads only).

    signed      a transaction's effect on one account: bank and cash count money in as +; a card counts
                debt as + (PRD §10.3), so a purchase adds and a payment or refund takes away
    anchors     the figures the app didn't compute: the opening balance (the balance at the START of
                its date, so that day's transactions come after it) and every balance observation
                (statement, email, manual checkpoint). A statement balance or a past-dated checkpoint is
                true at the END of its India date; an email balance or a checkpoint entered today is true
                at its moment: same-day transactions count before it by their own time when known, else
                by whether the app had recorded them yet (review finding, 2026-10-20)
    figure      the latest anchor plus the movements since: "₹84,200, as of 12 Aug, plus 34 since"
    checks      each observation against the anchor just before it plus the movements between them.
                Checking from the previous anchor (not from the opening plus everything) is the
                re-anchoring of §8.4: the next statement starts from the bank's own figure, so a gap
                stays on the interval where it arose instead of rolling into every later month or
                disappearing when a new statement arrives (ruling 2026-10-20)

An observation dated before the opening balance has nothing to be checked from and is left out.
Everything reads through transactions_effective, so the user's edits count.
"""

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from hisaab.clock import LOCAL_TZ
from hisaab.models import Account, BalanceDelta, BalanceObservation, TransactionEffective

END_OF_DAY: str = "23:59:59"  # statement balances and past-dated checkpoints are stored at this local time


@dataclass(frozen=True)
class Anchor:
    on: str  # India local date, YYYY-MM-DD
    balance: int  # paise; a card's is what's owed (positive)
    source: str  # opening | statement | email | manual
    observation_id: str | None  # None for the opening balance
    starts_day: bool  # the opening balance is the start of `on`; an observation is later that day
    at_time: str | None = None  # an observation inside its day (an email, today's checkpoint): its India time;
                                # None: the end of the day (a statement, a past-dated checkpoint)
    created_at: str = ""  # when it was recorded: orders untimed same-day transactions around it


@dataclass(frozen=True)
class Figure:
    anchor: Anchor | None  # the latest anchor; None: nothing to start from
    balance: int | None  # anchor + movements since
    transactions_since: int
    movement_since: int


@dataclass(frozen=True)
class Check:
    observation_id: str
    observed_on: str
    observed: int
    since: Anchor  # the anchor before it; its date is where the interval (and any gap) starts
    expected: int  # since.balance + the movements between the two
    gap: int  # observed - expected
    observed_source: str = "statement"  # statement | email | manual: who produced the observed figure


@dataclass(frozen=True)
class _Move:
    day: str
    time: str | None  # the transaction's own time of day, when the source gave one
    created_at: str  # when it was recorded
    amount: int  # signed for this account
    txn_id: str


def signed(account: Account, txn: Any) -> int:
    """`txn` is a TransactionEffective (or anything with legs and an amount)."""
    effect: int = (txn.amount if txn.to_account_id == account.id else 0) - (
        txn.amount if txn.from_account_id == account.id else 0)
    return -effect if account.type == "credit_card" else effect


def _local(observed_at: str) -> tuple[str, str | None]:
    """An observation's India date and time of day (None: the end of the day). observed_at is a UTC instant
    ('...Z'), or already a bare date."""
    if len(observed_at) == 10:
        return observed_at, None
    local: datetime = datetime.fromisoformat(observed_at).astimezone(LOCAL_TZ)
    clock: str = local.strftime("%H:%M:%S")
    return local.date().isoformat(), None if clock == END_OF_DAY else clock


def local_date(observed_at: str) -> str:
    return _local(observed_at)[0]


def _moves(session: Session, user_id: str, account: Account) -> list[_Move]:
    rows: list[TransactionEffective] = list(session.scalars(select(TransactionEffective).where(
        TransactionEffective.user_id == user_id,
        or_(TransactionEffective.from_account_id == account.id, TransactionEffective.to_account_id == account.id))
        .order_by(TransactionEffective.transaction_date)))
    return [_Move(t.transaction_date, t.transaction_time, t.created_at, signed(account, t), t.id) for t in rows]


def _before(m: _Move, a: Anchor) -> bool:
    """Whether the movement happened before the anchor's figure was true."""
    if a.starts_day or m.day != a.on:
        return m.day < a.on
    if a.at_time is None:
        return True  # an end-of-day balance includes everything on its date
    if m.time is not None:
        return m.time <= a.at_time
    return m.created_at <= a.created_at  # untimed: what the app already had when the balance was recorded


def anchors(session: Session, user_id: str, account: Account) -> list[Anchor]:
    """The anchors checks and the figure use, in time order: the opening balance, then observations (on one
    date, those inside the day by time, then end-of-day ones, each in the order recorded)."""
    found: list[tuple[tuple[str, int, str, str], Anchor]] = []
    if account.opening_balance is not None and account.opening_balance_date is not None:
        found.append(((account.opening_balance_date, 0, "", ""),
                      Anchor(account.opening_balance_date, account.opening_balance, "opening", None, True)))
    for o in session.scalars(select(BalanceObservation).where(
            BalanceObservation.user_id == user_id, BalanceObservation.account_id == account.id)):
        if account.type == "credit_card" and o.source == "email":
            continue  # the email contract's "available balance" is a card's credit left, not what is owed
        day, at = _local(o.observed_at)
        found.append(((day, 1, at or "~", o.created_at), Anchor(day, o.balance, o.source, o.id, False, at,
                                                                o.created_at)))
    found.sort(key=lambda pair: pair[0])
    ordered: list[Anchor] = [a for _, a in found]
    opening: Anchor | None = next((a for a in ordered if a.source == "opening"), None)
    return [a for a in ordered if opening is None or a.on >= opening.on]  # before the opening: nothing to check from


def _intervals(session: Session, user_id: str, account: Account,
               found: list[Anchor]) -> tuple[list[int], list[int]]:
    """Per anchor index j, the sum and count of movements between anchor j-1 and anchor j; index len(found)
    holds the movements after the last anchor. An accepted adjustment belongs to the interval of the
    balance it was written for, whatever its date (two balances can share one)."""
    pinned: dict[str, int] = {}
    index: dict[str, int] = {a.observation_id: j for j, a in enumerate(found) if a.observation_id}
    for txn_id, obs_id in session.execute(select(BalanceDelta.adjustment_transaction_id, BalanceDelta.observation_id)
                                          .where(BalanceDelta.user_id == user_id,
                                                 BalanceDelta.account_id == account.id,
                                                 BalanceDelta.status == "accepted",
                                                 BalanceDelta.adjustment_transaction_id.is_not(None))):
        if obs_id in index:
            pinned[txn_id] = index[obs_id]
    days: list[str] = [a.on for a in found]
    sums: list[int] = [0] * (len(found) + 1)
    counts: list[int] = [0] * (len(found) + 1)
    for m in _moves(session, user_id, account):
        j: int | None = pinned.get(m.txn_id)
        if j is None:
            j = bisect_left(days, m.day)  # anchors dated before the movement can't contain it
            while j < len(found) and not _before(m, found[j]):
                j += 1
        sums[j] += m.amount
        counts[j] += 1
    return sums, counts


def figure(session: Session, user_id: str, account: Account) -> Figure:
    found: list[Anchor] = anchors(session, user_id, account)
    if not found:
        return Figure(None, None, 0, 0)
    sums, counts = _intervals(session, user_id, account, found)
    latest: Anchor = found[-1]
    return Figure(latest, latest.balance + sums[-1], counts[-1], sums[-1])


def checks(session: Session, user_id: str, account: Account) -> list[Check]:
    found: list[Anchor] = anchors(session, user_id, account)
    sums, _ = _intervals(session, user_id, account, found)
    out: list[Check] = []
    for j in range(1, len(found)):
        obs: Anchor = found[j]
        assert obs.observation_id is not None  # only the opening has none, and it sorts first
        expected: int = found[j - 1].balance + sums[j]
        out.append(Check(obs.observation_id, obs.on, obs.balance, found[j - 1], expected, obs.balance - expected,
                         obs.source))
    return out
