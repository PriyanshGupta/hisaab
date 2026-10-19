"""Auto-categorize (slice 17, the user's brief): payees, not transactions, in one batched model call.

Per payee, because category is a rule resolved at read time (PRD §3.11): one
row covers that payee's past and future transactions, a wrong one is fixed in
one place, and the overlay keeps holding only the user's own decisions.

1. Candidates: payees with an uncategorized transaction of this user, no
   payee_categories row for the user, and no earlier answer for the current
   category list and prompt. That includes payees not yet named, known only by
   the text alerts printed ("raw:JioHotstar"; user, 2026-10-19: naming is
   optional); their answers are remembered by that text (raw_payee_suggestions),
   and a confident one names the payee after its text before filing it. So a
   second click costs nothing.
2. The prompt: the user's active categories (id, name) and per payee its name,
   up to 5 raw strings seen, the transaction count and the amount range.
3. One call per 100 payees. The model may search the web for payees it
   doesn't recognise (Anthropic's server-side search), then answers with a
   category id from the list or null ("none of these fit"), and a confidence in
   [0, 1]. An unknown category or an odd confidence counts as null: a category
   is never invented. Answers are remembered against the category list AND the
   prompt version, so improving the prompt asks again about unfiled payees.
4. Every answer is remembered; answers at or above
   auto_categorize_min_confidence_percent become rules (origin 'llm', one
   batch_id), unless the user wrote a rule for that payee meanwhile.

The model call runs outside the writer lock; the read before it and the write
after it are short. Payee strings go to the model at runtime only, the same
trust boundary as tier-2 email reading.
"""

import hashlib
import json
import statistics
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Subquery, or_, select
from sqlalchemy.orm import Session

from hisaab import llm
from hisaab.categories import repository as cats
from hisaab.categories.resolution import categorized
from hisaab.config import Settings
from hisaab.models import Category, PayeeCategory, PayeeSuggestion, RawPayeeSuggestion, new_id, utc_now
from hisaab.payees import repository as payees
from hisaab.pipeline.resolve import name_text
from hisaab.tunables import tunable

CATEGORIZABLE: tuple[str, ...] = ("expense", "income", "refund")
RAW: str = "raw:"  # a candidate known only by its text (no payee yet): "raw:<text>"
BATCH: int = 100
RAW_STRINGS: int = 5
TOOL: str = "suggest_categories"
PROMPT_VERSION: int = 2  # raise when the prompt improves: answers given under an older one are asked again
SEARCHES: int = 5  # web searches the model may make per call, for payees it doesn't recognise
MAX_TOKENS: int = 16000  # reasoning, searches and 100 answer lines must fit: a cut-off answer stores nothing
SYSTEM: str = """You file an Indian household's payees under the household's own spending categories.

You get the household's category list (id and name) and, for each payee: its name, the raw text bank alerts
printed for it, how many transactions it has, and their amount range in rupees.

For every payee, choose the ONE category from the list that a person in India would file it under, by what the
business mainly does. Match by meaning, not by wording: the household's category names are their own. Most
payees are well-known Indian businesses; file them with confidence. For example:
- Uber, Ola, Rapido, metro and fuel: transport
- Zomato, Swiggy, restaurants, cafes, bakeries: food or dining out
- Blinkit, Zepto, Instamart, BigBasket, DMart, Smart Bazaar: groceries
- Netflix, JioHotstar, Prime Video, Spotify, BookMyShow, PVR INOX, game stores: entertainment
- Jio, Airtel, Vi, electricity, gas, water, broadband, and home services such as TumbleDry (laundry) and
  Urban Company (cleaning, repairs): bills and utilities
- Pharmacies, clinics, hospitals, diagnostics: health
- Amazon, Flipkart, Myntra, Ajio, Nykaa: shopping
- MakeMyTrip, IRCTC, airlines, hotels: travel (or transport, if there is no travel category)

If you don't recognise a payee, or aren't sure what it sells, search the web for it (name plus "India") before
answering. When you have an answer for every payee, call suggest_categories once with all of them.

Use only category ids from the list; never make one up. Give null when none of the categories fits, or when
there is nothing to go on: a person's name or a UPI ID with no hint of what was paid for.

Confidence is between 0 and 1: how sure you are a careful person in this household would file it there. A
well-known business whose main line plainly matches a category is 0.9 or more. Use lower values only for real
doubt, such as a business that sells many kinds of things or a name you couldn't identify."""
# strict (the API holds the answer to this shape): every object closed, no numeric bounds (checked in ask)
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"suggestions": {"type": "array", "items": {
        "type": "object",
        "properties": {"payee_id": {"type": "string", "description": "the payee's id from the list, e.g. p1"},
                       "category_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                       "confidence": {"type": "number", "description": "0 to 1"}},
        "required": ["payee_id", "category_id", "confidence"],
        "additionalProperties": False}}},
    "required": ["suggestions"],
    "additionalProperties": False,
}


@dataclass
class Candidate:
    payee_id: str
    name: str
    raw_strings: list[str]
    amounts: list[int]  # paise, each categorizable transaction of the user's with this payee
    uncategorized: int  # of those, how many a rule would file (the rest carry an override)


@dataclass
class Plan:
    """What one click would send: read under no lock, before the model call."""
    categories: list[tuple[str, str]]  # (id, name), active
    categories_hash: str
    threshold: int  # percent
    candidates: list[Candidate]


@dataclass
class Applied:
    payee_id: str
    payee_name: str
    category_id: str
    confidence_percent: int
    transactions: int


def _hash(category_ids: Sequence[str]) -> str:
    """What an answer was given against: the category list, and the prompt (a better prompt asks again)."""
    return hashlib.sha256(f"v{PROMPT_VERSION}:{','.join(sorted(category_ids))}".encode()).hexdigest()


def plan(session: Session, user_id: str) -> Plan:
    categories: list[tuple[str, str]] = [(c.id, c.name) for c in cats.list_categories(session, user_id)
                                         if c.archived_at is None]
    digest: str = _hash([c for c, _ in categories])
    q: Subquery = categorized(user_id).subquery()
    rows: list[Any] = list(session.execute(select(q.c.payee_id, q.c.payee_name, q.c.payee_raw, q.c.amount,
                                                  q.c.category_id)
                                           .where(or_(q.c.payee_id.is_not(None), q.c.payee_raw.is_not(None)),
                                                  q.c.kind.in_(CATEGORIZABLE))))
    ruled: set[str] = set(session.scalars(select(PayeeCategory.payee_id).where(PayeeCategory.user_id == user_id)))
    asked: set[str] = set(session.scalars(select(PayeeSuggestion.payee_id).where(
        PayeeSuggestion.user_id == user_id, PayeeSuggestion.categories_hash == digest)))
    asked |= {RAW + r for r in session.scalars(select(RawPayeeSuggestion.payee_raw).where(
        RawPayeeSuggestion.user_id == user_id, RawPayeeSuggestion.categories_hash == digest))}
    by_payee: defaultdict[str, list[Any]] = defaultdict(list)
    for r in rows:  # a payee not yet named is known by its text: "raw:<text>"
        by_payee[r.payee_id or RAW + r.payee_raw].append(r)
    open_payees: set[str] = {k for k, rs in by_payee.items() if any(r.category_id is None for r in rs)} - ruled - asked
    candidates: list[Candidate] = [
        Candidate(k, rs[0].payee_name or rs[0].payee_raw or "", sorted({r.payee_raw for r in rs if r.payee_raw})[:RAW_STRINGS],
                  [r.amount for r in rs], sum(r.category_id is None for r in rs))
        for k, rs in sorted(by_payee.items(), key=lambda kv: (-len(kv[1]), kv[0])) if k in open_payees]
    return Plan(categories, digest, tunable(session, "auto_categorize_min_confidence_percent", user_id), candidates)


def prompt(categories: list[tuple[str, str]], batch: list[Candidate]) -> str:
    """Payees go by short references ("p1"): each answer line repeats one, and a long id or a long UPI string
    there would multiply the output (a 100-payee answer was cut off at the limit)."""
    return json.dumps({
        "categories": [{"id": c, "name": n} for c, n in categories],
        "payees": [{"id": f"p{i}", "name": c.name, "raw_strings": c.raw_strings,
                    "transactions": len(c.amounts), "amount_min_rupees": min(c.amounts) // 100,
                    "amount_max_rupees": max(c.amounts) // 100,
                    "amount_typical_rupees": int(statistics.median(c.amounts)) // 100} for i, c in enumerate(batch, 1)],
    }, ensure_ascii=False)


def ask(settings: Settings, p: Plan, transport: llm.Transport,
        sleep: Callable[[float], None]) -> dict[str, tuple[str | None, int]]:
    """The model's answer for every candidate: payee id (or "raw:<text>") -> (category id or None, confidence
    percent). Raises LLMUnavailable or ContractError, having stored nothing."""
    valid: set[str] = {c for c, _ in p.categories}
    out: dict[str, tuple[str | None, int]] = {}
    for start in range(0, len(p.candidates), BATCH):
        batch: list[Candidate] = p.candidates[start:start + BATCH]
        reply: dict[str, Any] = llm.tool_call(settings, SYSTEM, TOOL, "File each payee under one category, or null.",
                                              SCHEMA, prompt(p.categories, batch), transport=transport, sleep=sleep,
                                              max_tokens=MAX_TOKENS, search_uses=SEARCHES, strict=True)
        refs: dict[str, str] = {f"p{i}": c.payee_id for i, c in enumerate(batch, 1)}
        wanted: set[str] = set(refs.values())
        suggestions: Any = reply.get("suggestions")
        if isinstance(suggestions, str):  # the live model sometimes sends the list as JSON text (2026-10-19)
            try:
                suggestions = json.loads(suggestions)
            except json.JSONDecodeError:
                suggestions = None
        usable: int = 0
        for s in suggestions if isinstance(suggestions, list) else []:
            given: Any = s.get("payee_id") if isinstance(s, dict) else None
            key: Any = refs.get(given, given) if isinstance(given, str) else None
            if key not in wanted:
                continue  # a payee it wasn't asked about
            confidence: Any = s.get("confidence")
            category: Any = s.get("category_id")
            sane: bool = type(confidence) in (int, float) and 0 <= confidence <= 1 and category in valid
            out[key] = (category, round(confidence * 100)) if sane else (None, 0)
            usable += 1
        if usable == 0:  # nothing to go on: store nothing, so the next click asks again
            raise llm.ContractError(f"the {TOOL} answer had no usable entry (suggestions was "
                                    f"{type(reply.get('suggestions')).__name__})")
        for c in batch:
            out.setdefault(c.payee_id, (None, 0))  # unanswered: remembered as "none fit", never re-sent
    return out


def store(session: Session, user_id: str, is_admin: bool, p: Plan,
          answers: dict[str, tuple[str | None, int]]) -> tuple[str, list[Applied]]:
    """Remember every answer; write the confident ones as rules. A payee not yet named is first named after its
    text (as choosing its category by hand does). Returns the batch id and what was applied."""
    batch_id: str = new_id()
    names: dict[str, Candidate] = {c.payee_id: c for c in p.candidates}
    applied: list[Applied] = []
    for key, (category_id, percent) in answers.items():
        raw: str | None = key.removeprefix(RAW) if key.startswith(RAW) else None
        memory: PayeeSuggestion | RawPayeeSuggestion = (
            RawPayeeSuggestion(user_id=user_id, payee_raw=raw, category_id=category_id, confidence_percent=percent,
                               categories_hash=p.categories_hash, batch_id=batch_id, created_at=utc_now())
            if raw is not None else
            PayeeSuggestion(user_id=user_id, payee_id=key, category_id=category_id, confidence_percent=percent,
                            categories_hash=p.categories_hash, batch_id=batch_id, created_at=utc_now()))
        session.merge(memory)
        if category_id is None or percent < p.threshold:
            continue
        category: Category | None = session.get(Category, category_id)
        if category is None or category.user_id != user_id or category.archived_at is not None:
            continue  # archived while the model was thinking: skip it, keep the rest of the batch
        payee_id: str = key
        if raw is not None:
            payee_id = payees.get_or_create_by_name(session, raw).id
            name_text(session, user_id, is_admin, raw, payee_id)
        if session.get(PayeeCategory, (user_id, payee_id)) is not None:
            continue  # the user wrote a rule while the model was thinking: theirs stands
        cats.set_payee_category(session, user_id, payee_id, category_id, origin="llm", batch_id=batch_id)
        applied.append(Applied(payee_id, names[key].name, category_id, percent, names[key].uncategorized))
    session.flush()
    return batch_id, applied


def declined(session: Session, user_id: str) -> int:
    """Payees answered for the current category list and prompt but not filed: left for the user."""
    digest: str = plan(session, user_id).categories_hash
    q: Subquery = categorized(user_id).subquery()
    open_rows: list[Any] = list(session.execute(select(q.c.payee_id, q.c.payee_raw).where(
        q.c.kind.in_(CATEGORIZABLE), q.c.category_id.is_(None))))
    open_named: set[str] = {r.payee_id for r in open_rows if r.payee_id}
    open_raw: set[str] = {r.payee_raw for r in open_rows if not r.payee_id and r.payee_raw}
    answered: set[str] = set(session.scalars(select(PayeeSuggestion.payee_id).where(
        PayeeSuggestion.user_id == user_id, PayeeSuggestion.categories_hash == digest)))
    answered_raw: set[str] = set(session.scalars(select(RawPayeeSuggestion.payee_raw).where(
        RawPayeeSuggestion.user_id == user_id, RawPayeeSuggestion.categories_hash == digest)))
    ruled: set[str] = set(session.scalars(select(PayeeCategory.payee_id).where(PayeeCategory.user_id == user_id)))
    return len((answered & open_named) - ruled) + len(answered_raw & open_raw)
