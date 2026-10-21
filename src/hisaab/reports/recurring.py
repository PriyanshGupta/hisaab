"""Recurring charges (PRD §7.8, §10.1; rulings 2026-10-20), computed at read time: no table, no job (§8.3).

A recurring charge is a run of 3+ expenses from one payee where
    amounts   each charge is within 10% of the one before it (in date order): a bill that drifts, or a
              small price rise, stays one charge; a bigger jump starts a new series
    rhythm    every gap is within ±4 days of the typical (median) gap, and the typical gap is at least a
              week: a fee on every daily order is "within 4 days" of a 1-day rhythm, and is not a
              subscription
Within a payee, charges are grouped by amount first (sorted amounts, a new group at every jump over
10%), because one payee often mixes a monthly charge with one-off purchases (an app store, a landlord
paid a small extra once). A weekly shop with varying amounts rarely forms a group of steady amounts with
a steady rhythm, and when a few similar amounts do land at a steady spacing, the shop's other visits inside
that span give it away (_coincidence). A series whose next charge is overdue has stopped and is not listed.
The thresholds are the tunable recurring_detection; the week floor is MIN_GAP_DAYS.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hisaab.clock import local_today
from hisaab.models import Account
from hisaab.models import TransactionEffective as T
from hisaab.transactions.repository import with_provenance
from hisaab.tunables import tunable

MIN_GAP_DAYS: int = 7
WINDOW_DAYS: int = 365
# Charges a year for the rhythms people name (gap range in days, charges per year): "about ₹7,788 a year"
# for ₹649 a month is 12 × 649, not 365 / 30 × 649. Any other gap: 365 // gap.
PER_YEAR: tuple[tuple[int, int, int], ...] = ((6, 8, 52), (13, 15, 26), (27, 33, 12), (57, 63, 6), (85, 95, 4),
                                               (175, 190, 2), (355, 375, 1))


@dataclass(frozen=True)
class Charge:
    txn_id: str
    on: str  # YYYY-MM-DD
    amount: int  # paise
    payee_key: str  # the payee's id, or its raw text when not named yet
    payee_name: str
    category_id: str | None
    account_label: str | None


@dataclass(frozen=True)
class Rules:
    min_occurrences: int = 3
    interval_days: int = 4
    amount_percent: int = 10
    min_gap_days: int = MIN_GAP_DAYS


@dataclass(frozen=True)
class Series:
    payee_name: str
    category_id: str | None  # the latest charge's category
    account_label: str | None  # the latest charge's account
    charges: tuple[Charge, ...]  # oldest first
    typical_gap_days: int
    latest_amount: int
    first_amount: int
    last_on: str
    next_on: str  # when the next one is due: last + the typical gap
    yearly_paise: int  # about what a year costs at the latest amount


def _within(before: int, after: int, percent: int) -> bool:
    return abs(after - before) * 100 <= before * percent


def _amount_groups(charges: list[Charge], percent: int) -> list[list[Charge]]:
    """Sorted by amount, a new group wherever an amount jumps by more than `percent`% over the previous."""
    groups: list[list[Charge]] = []
    for c in sorted(charges, key=lambda c: (c.amount, c.on)):
        if groups and c.amount * 100 <= groups[-1][-1].amount * (100 + percent):
            groups[-1].append(c)
        else:
            groups.append([c])
    return groups


def _per_year(gap: int) -> int | None:
    return next((n for lo, hi, n in PER_YEAR if lo <= gap <= hi), None)


def _series(group: list[Charge], rules: Rules) -> Series | None:
    if len(group) < rules.min_occurrences:
        return None
    run: list[Charge] = sorted(group, key=lambda c: (c.on, c.txn_id))
    if not all(_within(a.amount, b.amount, rules.amount_percent) for a, b in zip(run, run[1:])):
        return None
    days: list[date] = [date.fromisoformat(c.on) for c in run]
    gaps: list[int] = [(b - a).days for a, b in zip(days, days[1:])]
    ordered: list[int] = sorted(gaps)
    lo, hi = ordered[(len(ordered) - 1) // 2], ordered[len(ordered) // 2]  # the median is (lo + hi) / 2, exactly
    if lo + hi < 2 * rules.min_gap_days or any(abs(2 * g - (lo + hi)) > 2 * rules.interval_days for g in gaps):
        return None
    typical: int = (lo + hi) // 2
    last: Charge = run[-1]
    per_year: int | None = _per_year(typical)
    return Series(payee_name=last.payee_name, category_id=last.category_id, account_label=last.account_label,
                  charges=tuple(run), typical_gap_days=typical, latest_amount=last.amount,
                  first_amount=run[0].amount, last_on=last.on,
                  next_on=(days[-1] + timedelta(days=typical)).isoformat(),
                  yearly_paise=last.amount * per_year if per_year else last.amount * 365 // typical)


def _coincidence(series: Series, payee_charges: list[Charge]) -> bool:
    """As many of the payee's other charges fall inside the series' span as the series has: a busy shop where
    a few similar amounts happened to land at a steady spacing, not a standing charge (review, 2026-10-20)."""
    mine: set[str] = {c.txn_id for c in series.charges}
    first, last = series.charges[0].on, series.charges[-1].on
    others: int = sum(1 for c in payee_charges if c.txn_id not in mine and first <= c.on <= last)
    return others >= len(series.charges)


def _stopped(series: Series, rules: Rules, today: date) -> bool:
    """Its next charge is overdue by more than the rhythm allows: cancelled, or replaced by a new price."""
    return today > date.fromisoformat(series.next_on) + timedelta(days=rules.interval_days)


def find_series(charges: Sequence[Charge], rules: Rules, today: date | None = None) -> list[Series]:
    """Recurring series among `charges`; with `today`, only those still running."""
    by_payee: dict[str, list[Charge]] = {}
    for c in charges:
        by_payee.setdefault(c.payee_key, []).append(c)
    found: list[Series] = []
    for payee_charges in by_payee.values():
        for group in _amount_groups(payee_charges, rules.amount_percent):
            series: Series | None = _series(group, rules)
            if series is None or _coincidence(series, payee_charges):
                continue
            if today is not None and _stopped(series, rules, today):
                continue
            found.append(series)
    return sorted(found, key=lambda s: (s.next_on, s.payee_name))


def rules_for(session: Session, user_id: str) -> Rules:
    raw: dict[str, Any] = tunable(session, "recurring_detection", user_id)
    return Rules(min_occurrences=int(raw["min_occurrences"]), interval_days=int(raw["interval_tolerance_days"]),
                 amount_percent=int(raw["amount_tolerance_percent"]))


def recurring(session: Session, user_id: str, today: date | None = None) -> list[Series]:
    """The user's recurring charges among expenses of the last 12 months up to today."""
    today = today or local_today()
    accounts: dict[str, str] = dict(session.execute(select(Account.id, Account.display_name).where(
        Account.user_id == user_id)).tuples().all())
    rows: list[Any] = list(session.execute(with_provenance(user_id).where(
        T.kind == "expense", T.status != "reversed",
        T.transaction_date > (today - timedelta(days=WINDOW_DAYS)).isoformat(),
        T.transaction_date <= today.isoformat())))
    charges: list[Charge] = []
    for row in rows:
        t: T = row.TransactionEffective
        raw: str = (t.payee_raw or "").strip()
        key: str = t.payee_id or (f"raw:{raw.lower()}" if raw else "")
        if not key:
            continue  # nothing says who was paid: can't tell one payee's rhythm from another's
        charges.append(Charge(txn_id=t.id, on=t.transaction_date, amount=t.amount, payee_key=key,
                              payee_name=row.payee_name or raw, category_id=row.category_id,
                              account_label=accounts.get(t.from_account_id or "")))
    return find_series(charges, rules_for(session, user_id), today)
