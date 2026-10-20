"""Template proposals (tech-design §4.10 steps 2-3): one Sonnet call per format that reached its threshold.

A sighting is ready when it is accumulating with `threshold` examples, and either has no candidate yet or its
last candidate (one that failed) was made from other examples. So Sonnet is asked once per set of examples:
a failure is retried only after a new example rotates in (sightings.py), and an outage (LLMUnavailable)
writes nothing, so the next run asks again.

Every example must still hold its email and its cached model reading. A set with a gap is skipped, never sent
short: three independent readings are what replay checks against.

The model fills a form (llm.PROPOSAL_SCHEMA); build_spec turns it into a §4.2 spec, with the sender fixed to
the sighting's exact address and the preprocessing fixed to strip_html then collapse_whitespace, which is the
text the model was shown. replay.replay validates it. Passing: the template is 'validated' and the sighting
'proposed', waiting for the admin (or approved at once when auto_promote_templates is on). Failing, an
invalid spec, or an unusable answer: a 'proposed' template with passed: false, shown in the queue as
discarded; the sighting keeps accumulating.

A candidate discarded by replay alone, whose examples are unchanged, is replayed again on every run (pure,
no model call): when validation itself was wrong (it once compared times to the second), the candidate
comes back as validated instead of waiting for a new example. One rejected by an admin never comes back.

Model calls run outside the writer lock: read, call, then one write that re-checks the sighting has not
moved on in the meantime.
"""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hisaab.config import Settings
from hisaab.jobs.queue import JobContext
from hisaab.llm import ContractError, LLMUnavailable, Transport, propose_template
from hisaab.models import ExtractionCache, FormatSighting, ParserTemplate, Source
from hisaab.parsing.email_interpreter import parse_eml
from hisaab.parsing.email_spec import CONTRACT_FIELDS
from hisaab.parsing.templates import save_candidate
from hisaab.promotion.promote import approve
from hisaab.promotion.replay import Example, replay
from hisaab.promotion.sightings import examples
from hisaab.tunables import tunable

PREPROCESS: list[str] = ["strip_html", "collapse_whitespace"]  # what llm.prompt_for shows the model
_log: logging.Logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Ready:
    sighting_id: str
    sender: str
    subject: str  # masked, for the log
    example_ids: list[str]
    items: list[Example]


def build_spec(answer: dict[str, Any], sender: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for name in CONTRACT_FIELDS:
        form: dict[str, Any] = answer["fields"][name]
        if form.get("kind") == "const":  # empty strings mean "none" in the form (llm.PROPOSAL_SCHEMA)
            fields[name] = {"const": form.get("value") or None}
        elif form.get("kind") == "pattern":
            fields[name] = {"pattern": form.get("pattern") or None,
                            **({"transform": form["transform"]} if form.get("transform") else {}),
                            **({} if form.get("required", True) else {"required": False})}
        else:
            fields[name] = {"required": False}
    return {"version": 1, "match": {"sender": sender, "subject": answer["subject_pattern"]},
            "preprocess": PREPROCESS, "fields": fields}


def ready(session: Session, row: FormatSighting, threshold: int) -> bool:
    ids: list[str] = examples(row)
    if row.state != "accumulating" or len(ids) < threshold:
        return False
    if row.template_id is None:
        return True
    last: ParserTemplate | None = session.get(ParserTemplate, row.template_id)
    return last is None or json.loads(last.example_source_ids or "[]") != ids


def _gather(session: Session, row: FormatSighting) -> _Ready | None:
    items: list[Example] = []
    for source_id in examples(row):
        source: Source | None = session.get(Source, source_id)
        cached: ExtractionCache | None = session.get(ExtractionCache, source.content_hash) if source else None
        if source is None or source.content is None or cached is None or cached.extracted is None:
            return None
        items.append(Example(source.id, parse_eml(source.content), json.loads(cached.extracted)))
    return _Ready(row.id, row.sender_address, row.subject_masked, examples(row), items)


def propose_ready(ctx: JobContext, settings: Settings, transport: Transport,
                  sleep: Callable[[float], None]) -> dict[str, int]:
    counts: dict[str, int] = {"proposed": 0, "validated": 0, "failed": 0, "auto_promoted": 0}
    if settings.anthropic_api_key is None:
        return counts
    with ctx.read() as s:
        threshold: int = tunable(s, "template_promotion_sightings")
        rows: list[FormatSighting] = list(s.scalars(select(FormatSighting).order_by(FormatSighting.first_seen_at)))
        waiting: list[_Ready] = [r for row in rows if ready(s, row, threshold) and (r := _gather(s, row)) is not None]
        recheck: list[tuple[_Ready, str, dict[str, Any]]] = [
            (r, t.id, json.loads(t.spec)) for row in rows if (t := _failed_candidate(s, row)) is not None
            and (r := _gather(s, row)) is not None]
    for item, template_id, old_spec in recheck:  # pure replay, no model call
        validation_now: dict[str, Any] = replay(old_spec, item.items)
        if validation_now["passed"]:
            with ctx.write() as s:
                if _revalidate(s, item, template_id, validation_now):
                    counts["validated"] += 1
                    if settings.auto_promote_templates:
                        approve(s, template_id, settings)
                        counts["auto_promoted"] += 1
    for item in waiting:
        ctx.checkpoint()
        try:
            answer: dict[str, Any] = propose_template(settings, [(e.artifact, e.expected) for e in item.items],
                                                      transport=transport, sleep=sleep)
            spec: dict[str, Any] = build_spec(answer, item.sender)
            validation: dict[str, Any] = replay(spec, item.items)
            name: str = answer.get("name") or "proposed"
        except LLMUnavailable as e:  # every other call would wait out the same retries; the next sync asks again
            _log.warning("template proposal for %s %r waits for the next sync: %s", item.sender, item.subject, e)
            break
        except ContractError as e:
            spec, name = {}, "proposed"
            validation = {"passed": False, "problems": [f"the model's answer was unusable: {e}"], "examples": []}
        with ctx.write() as s:
            template: ParserTemplate | None = _store(s, item, spec, name, validation, threshold)
            if template is not None:
                counts["proposed"] += 1
                counts["validated" if validation["passed"] else "failed"] += 1
                if validation["passed"] and settings.auto_promote_templates:  # off by default (§4.10)
                    approve(s, template.id, settings)
                    counts["auto_promoted"] += 1
    return counts


def _failed_candidate(session: Session, row: FormatSighting) -> ParserTemplate | None:
    """The sighting's discarded candidate, if replay (not an admin) discarded it and its examples are unchanged:
    one a fixed validation may now pass. A rejected or spec-less candidate never comes back."""
    if row.state != "accumulating" or row.template_id is None:
        return None
    template: ParserTemplate | None = session.get(ParserTemplate, row.template_id)
    if template is None or template.status != "proposed" or template.spec in ("{}", ""):
        return None
    validation: dict[str, Any] = json.loads(template.validation_result or "{}")
    if validation.get("rejected") or json.loads(template.example_source_ids or "[]") != examples(row):
        return None
    return template


def _revalidate(session: Session, item: _Ready, template_id: str, validation: dict[str, Any]) -> bool:
    row: FormatSighting | None = session.get(FormatSighting, item.sighting_id)
    template: ParserTemplate | None = session.get(ParserTemplate, template_id)
    if row is None or template is None or _failed_candidate(session, row) is None or examples(row) != item.example_ids:
        return False  # it moved on in the meantime
    template.status = "validated"
    template.validation_result = json.dumps(validation, ensure_ascii=False)
    row.state = "proposed"
    session.flush()
    return True


def _store(session: Session, item: _Ready, spec: dict[str, Any], name: str, validation: dict[str, Any],
           threshold: int) -> ParserTemplate | None:
    row: FormatSighting | None = session.get(FormatSighting, item.sighting_id)
    if row is None or not ready(session, row, threshold) or examples(row) != item.example_ids:
        return None  # it moved on while the model was thinking
    if row.template_id is not None:  # the failed candidate this one replaces
        old: ParserTemplate | None = session.get(ParserTemplate, row.template_id)
        row.template_id = None
        if old is not None and old.status in ("proposed", "validated"):
            session.flush()
            session.delete(old)
    template: ParserTemplate = save_candidate(session, name=_unique(session, name), spec=spec,
                                              example_source_ids=item.example_ids, validation=validation)
    row.template_id = template.id
    row.state = "proposed" if validation["passed"] else "accumulating"
    session.flush()
    return template


def _unique(session: Session, wanted: str) -> str:
    """A name no template has: sync-seeds upserts by name, so a clash could overwrite a seed or be overwritten."""
    base: str = re.sub(r"[^a-z0-9_]+", "_", wanted.lower()).strip("_")[:60] or "proposed"
    taken: set[str] = set(session.scalars(select(ParserTemplate.name).where(ParserTemplate.name.like(f"{base}%"))))
    n: int = 1
    name: str = base
    while name in taken:
        n += 1
        name = f"{base}_{n}"
    return name
