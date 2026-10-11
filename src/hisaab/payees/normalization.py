"""Raw payee strings to Payees (tech-design §6.4, PRD §5.6).

normalization_patterns rows are tried in priority order, highest first, and
the first match wins. Priority is explicit because contains-patterns overlap:
"Swiggy" and "Swiggy Instamart" both match "SWIGGY INSTAMART BLR", and only an
explicit order says which payee that is. Equal priorities fall back to the
pattern id, so the answer never depends on row order.

Match types:
- exact: the whole string, ignoring case, spaces and punctuation
- contains: the pattern appears in the string, ignoring case, spaces and
  punctuation, so "Best Buy" matches the squashed "BestBuySanfordUs" a PDF
  statement prints
- regex: an RE2 pattern searched in the raw string as received

The pipeline applies this at derive time (slice 14); payee_raw stays on the
transaction, so a new pattern can be applied to history without a re-fetch.
"""

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from hisaab.models import NormalizationPattern
from hisaab.parsing import patterns

_NOT_ALNUM: re.Pattern[str] = re.compile(r"[^0-9a-z]")


@dataclass(frozen=True)
class PatternRule:
    id: str
    match_type: str  # exact | contains | regex
    pattern: str
    payee_id: str
    priority: int


def _squash(text: str) -> str:
    return _NOT_ALNUM.sub("", text.lower())


def _matches(rule: PatternRule, raw: str) -> bool:
    if rule.match_type == "regex":
        try:
            return patterns.search(patterns.compile_pattern(rule.pattern), raw) is not None
        except (patterns.PatternError, patterns.PatternTimeout):
            return False
    wanted: str = _squash(rule.pattern)
    if not wanted:  # a pattern of only punctuation would match everything
        return False
    return _squash(raw) == wanted if rule.match_type == "exact" else wanted in _squash(raw)


def match_payee(rules: list[PatternRule], raw: str) -> str | None:
    """The payee id of the first rule (priority DESC, then id) that matches raw, or None."""
    for rule in sorted(rules, key=lambda r: (-r.priority, r.id)):
        if _matches(rule, raw):
            return rule.payee_id
    return None


def load_rules(session: Session) -> list[PatternRule]:
    """Every normalization pattern (global, tech-design §3.3), ready for match_payee."""
    return [PatternRule(p.id, p.match_type, p.pattern, p.payee_id, p.priority)
            for p in session.scalars(select(NormalizationPattern))]
