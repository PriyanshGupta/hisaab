"""Sightings (tech-design §4.10 steps 1-2): every email read by the model is counted under its fingerprint.

    accumulating  the first `threshold` sightings Haiku read as a transaction are the examples, in order;
                  a non-transactional reading counts but is no example, and two of them exclude the format,
                  unless any email of it was read as a transaction (review fix: two misreadings must not
                  drop a transactional format). An admin can include an excluded format again.
    proposed      a candidate passed replay and awaits the admin: sightings only count
    promoted      its template is active: sightings only count; if the template was demoted, the format
                  starts over as accumulating with this email as its first example (§4.10 demotion)
    excluded_non_transactional
                  sightings count, and the email's stored content goes: nothing will ever replay it

An unparseable reading is not counted: there is nothing to replay a candidate against.

When a candidate failed (the sighting is back to accumulating, template_id still on the failed row), a new
transactional sighting replaces the oldest example, whose stored content then goes, so the next proposal
sees a different set (§4.10 step 3: "it retries when a fourth example arrives").

The non-transactional count is read from the Sources themselves (diagnostic.fingerprint), so it needs no
column. Sources keep the fingerprint in their diagnostic: it is how promotion finds every cached reading of
the format (§4.1), and how a re-read of an already counted email is told apart from a new one.
"""

import json
from typing import Any

from sqlalchemy import ScalarResult, func, select
from sqlalchemy.orm import Session

from hisaab.models import ExtractionCache, FormatSighting, ParserTemplate, Source, utc_now
from hisaab.parsing.email_interpreter import parse_eml
from hisaab.parsing.fingerprint import Fingerprint, fingerprint
from hisaab.tunables import tunable

NON_TXN_EXCLUDE: int = 2  # §4.10: two, not three; a bank's promotional template is unambiguous


def examples(row: FormatSighting) -> list[str]:
    ids: list[str] = json.loads(row.example_source_ids)
    return ids


def excluded(session: Session, value: str) -> bool:
    return session.scalar(select(FormatSighting.state).where(FormatSighting.fingerprint == value)) \
        == "excluded_non_transactional"


def with_fingerprint(value: str) -> Any:
    """SQL condition: Sources whose diagnostic records this fingerprint."""
    return func.json_extract(Source.diagnostic, "$.fingerprint") == value


def record_sighting(session: Session, fp: Fingerprint, source: Source, outcome: str,
                    threshold: int) -> FormatSighting | None:
    """Count one model-read email. The Source's diagnostic must already carry fp.value."""
    if outcome not in ("transaction", "non_transactional"):
        return None
    now: str = utc_now()
    row: FormatSighting | None = session.scalars(
        select(FormatSighting).where(FormatSighting.fingerprint == fp.value)).first()
    if row is None:
        row = FormatSighting(fingerprint=fp.value, sender_address=fp.sender, subject_masked=fp.subject_masked,
                             structural_markers=json.dumps({"static_lines": fp.static_lines, "bucket": fp.bucket}),
                             sighting_count=1, example_source_ids="[]", state="accumulating",
                             first_seen_at=now, last_seen_at=now)
        session.add(row)
    else:
        row.sighting_count += 1
        row.last_seen_at = now
    if row.state == "promoted":
        template: ParserTemplate | None = session.get(ParserTemplate, row.template_id) if row.template_id else None
        if template is not None and template.status != "demoted":
            return row
        row.state, row.template_id, row.example_source_ids = "accumulating", None, "[]"
    if row.state == "excluded_non_transactional":
        source.content = None
    elif row.state == "accumulating" and outcome == "transaction":
        _add_example(session, row, source, threshold)
    elif row.state == "accumulating":
        session.flush()
        if _non_transactional(session, fp.value) >= NON_TXN_EXCLUDE and not _has_transactions(session, fp.value):
            row.state = "excluded_non_transactional"
            for other in session.scalars(select(Source).where(with_fingerprint(fp.value),
                                                              Source.processing_state == "non_transactional")):
                other.content = None
    session.flush()
    return row


def _add_example(session: Session, row: FormatSighting, source: Source, threshold: int) -> None:
    ids: list[str] = examples(row)
    if source.id in ids:
        return
    if len(ids) < threshold:
        ids.append(source.id)
    elif row.template_id is not None:  # the last candidate failed: rotate the oldest example out
        oldest: Source | None = session.get(Source, ids.pop(0))
        if oldest is not None:
            oldest.content = None
        ids.append(source.id)
    row.example_source_ids = json.dumps(ids)


def _has_transactions(session: Session, value: str) -> bool:
    """Any email of the format read as a transaction: then it is never excluded (a misreading or two must not
    make every later transaction of the format vanish)."""
    return session.scalar(select(Source.id).where(with_fingerprint(value), Source.processing_state == "parsed")
                          .limit(1)) is not None


class NotExcluded(LookupError):
    pass


def include_again(session: Session, sighting_id: str) -> None:
    """An admin's answer to a wrong exclusion: the format's emails go back to the model and are counted again."""
    row: FormatSighting | None = session.get(FormatSighting, sighting_id)
    if row is None or row.state != "excluded_non_transactional":
        raise NotExcluded("No such excluded format.")
    row.state = "accumulating"
    session.flush()


def _non_transactional(session: Session, value: str) -> int:
    return session.scalar(select(func.count()).select_from(Source).where(
        with_fingerprint(value), Source.processing_state == "non_transactional")) or 0


def sight_retained(session: Session, user_id: str, threshold: int | None = None) -> int:
    """Count this user's retained emails the model read before sightings existed (or that were never counted),
    oldest first, from their cached reading: no model call. Returns how many were counted."""
    rows: ScalarResult[Source] = session.scalars(
        select(Source).where(Source.user_id == user_id, Source.kind == "email", Source.content.is_not(None),
                             Source.processing_state.in_(("parsed", "non_transactional")),
                             func.json_extract(Source.diagnostic, "$.fingerprint").is_(None))
        .order_by(Source.ingest_seq))
    limit: int = threshold if threshold is not None else tunable(session, "template_promotion_sightings")
    counted: int = 0
    for source in list(rows):
        cached: ExtractionCache | None = session.get(ExtractionCache, source.content_hash)
        if cached is None or cached.tier != 2 or source.content is None:
            continue
        fp: Fingerprint = fingerprint(parse_eml(source.content))
        source.diagnostic = json.dumps({**json.loads(source.diagnostic or "{}"), "fingerprint": fp.value})
        if record_sighting(session, fp, source, cached.outcome, limit) is not None:
            counted += 1
    return counted
