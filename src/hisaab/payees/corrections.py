"""Captured payee corrections and their promotion (PRD §5.1 Loop 1, §5.6; tech-design §6.4).

A captured correction is a user's payee edit on a transaction that carries raw payee text an alert or statement
printed: a member's "name this payee" (edits on their own rows, ruling 2026-10-12) or anyone's "Payee for this
one". Typed entries are left out: their payee_raw is the name the user typed, not something to normalize. Nothing new is
stored to capture them: they are read from transaction_edits, grouped by the raw text squashed the way exact
patterns match (lower-case letters and digits only) and by the payee chosen. Each shows the raw text, the
chosen name, how many transactions across every user share that text, and who reported it.

A correction stops showing once a pattern already maps its text to that payee, or once the admin dismisses it
(correction_dismissals, the only thing stored).

promote writes one global pattern (exact by default; contains or regex if the admin edits it first), at the
priority an admin's naming uses, and applies it to history in the database alone (pipeline.resolve
.reapply_pattern): payee_raw is kept on every transaction precisely so a new pattern needs no re-fetch.
Patterns get the same safeguards as templates (PRD §5.6, §8.5): a regex must compile under RE2 within the
length cap; an exact or contains pattern must keep some letters or digits.
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from hisaab.models import CorrectionDismissal, NormalizationPattern, Payee, Source, Transaction, TransactionEdit, User
from hisaab.parsing import patterns
from hisaab.payees import repository as payees
from hisaab.payees.normalization import PatternRule, load_rules, match_payee
from hisaab.pipeline.resolve import PATTERN_PRIORITY, reapply_pattern

MATCH_TYPES: tuple[str, ...] = ("exact", "contains", "regex")
_NOT_ALNUM: re.Pattern[str] = re.compile(r"[^0-9a-z]")


class InvalidPattern(ValueError):
    pass


@dataclass(frozen=True)
class Correction:
    key: str  # the raw text squashed: what groups and dismisses
    raw: str  # the raw text as most of its rows print it
    payee_id: str
    payee_name: str
    shared: int  # transactions across every user carrying this text
    reporters: list[tuple[str, int]]  # (display name, how many of their transactions they corrected)


@dataclass(frozen=True)
class PromoteResult:
    pattern_id: str
    named: int  # transactions whose payee changed
    users: int  # how many people those belong to


def squash(raw: str) -> str:
    return _NOT_ALNUM.sub("", raw.lower())


def corrections(session: Session) -> list[Correction]:
    edits: list[tuple[str, str, str]] = [  # (raw text, chosen payee, who)
        (raw, payee_id, user_id) for raw, payee_id, user_id in session.execute(
            select(Transaction.payee_raw, TransactionEdit.new_value, TransactionEdit.user_id)
            .join(Transaction, Transaction.id == TransactionEdit.transaction_id)
            .join(Source, Source.id == Transaction.primary_source_id)
            .where(TransactionEdit.field == "payee_id", TransactionEdit.new_value.is_not(None),
                   Transaction.payee_raw.is_not(None), Source.kind != "manual")).tuples()
        if raw and squash(raw) and payee_id]
    if not edits:
        return []
    shared: Counter[str] = Counter(squash(r) for r in session.scalars(
        select(Transaction.payee_raw).join(Source, Source.id == Transaction.primary_source_id)
        .where(Transaction.payee_raw.is_not(None), Source.kind != "manual")) if r)
    names: dict[str, str] = dict(session.execute(select(User.id, User.display_name)).tuples().all())
    payee_names: dict[str, str] = dict(session.execute(select(Payee.id, Payee.normalized_name)).tuples().all())
    dismissed: set[tuple[str, str]] = set(session.execute(
        select(CorrectionDismissal.raw_key, CorrectionDismissal.payee_id)).tuples().all())
    rules: list[PatternRule] = load_rules(session)
    groups: defaultdict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for raw, payee_id, user_id in edits:
        groups[(squash(raw), payee_id)].append((raw, user_id))
    found: list[Correction] = []
    for (key, payee_id), rows in groups.items():
        raw = Counter(r for r, _ in rows).most_common(1)[0][0]
        if (key, payee_id) in dismissed or payee_id not in payee_names or match_payee(rules, raw) == payee_id:
            continue
        by_user: Counter[str] = Counter(u for _, u in rows)
        found.append(Correction(key, raw, payee_id, payee_names[payee_id], shared[key],
                                sorted(((names.get(u, "?"), n) for u, n in by_user.items()), key=lambda r: (-r[1], r[0]))))
    return sorted(found, key=lambda c: (-c.shared, c.raw))


def promote(session: Session, *, payee_id: str | None, payee_name: str | None, match_type: str,
            pattern: str) -> PromoteResult:
    _check(match_type, pattern)
    if (payee_id is None) == (payee_name is None or not payee_name.strip()):
        raise InvalidPattern("Choose an existing payee or type a new name.")
    payee: Payee = payees.get_payee(session, payee_id) if payee_id else payees.get_or_create_by_name(session, payee_name or "")
    row: NormalizationPattern = NormalizationPattern(match_type=match_type, pattern=pattern.strip(), payee_id=payee.id,
                                                     origin="admin", priority=PATTERN_PRIORITY)
    session.add(row)
    session.flush()
    touched: dict[str, list[str]] = reapply_pattern(
        session, PatternRule(row.id, match_type, row.pattern, payee.id, PATTERN_PRIORITY))
    return PromoteResult(row.id, sum(len(ids) for ids in touched.values()), len(touched))


def dismiss(session: Session, raw_key: str, payee_id: str, user_id: str) -> None:
    exists: CorrectionDismissal | None = session.scalars(select(CorrectionDismissal).where(
        CorrectionDismissal.raw_key == raw_key, CorrectionDismissal.payee_id == payee_id)).first()
    if exists is None:
        session.add(CorrectionDismissal(raw_key=raw_key, payee_id=payee_id, dismissed_by=user_id))
        session.flush()


def _check(match_type: str, pattern: str) -> None:
    if match_type not in MATCH_TYPES:
        raise InvalidPattern(f"The match type must be one of: {', '.join(MATCH_TYPES)}.")
    if match_type == "regex":
        try:
            patterns.compile_pattern(pattern, groups=0)
        except patterns.PatternError as e:
            raise InvalidPattern(f"That regular expression can't be used: {e}") from None
        return
    if not squash(pattern):
        raise InvalidPattern("The pattern needs some letters or digits.")
    if len(pattern) > patterns.MAX_PATTERN_LENGTH:
        raise InvalidPattern(f"The pattern is longer than {patterns.MAX_PATTERN_LENGTH} characters.")
