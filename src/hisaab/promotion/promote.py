"""Approving and rejecting a proposed template (tech-design §4.10 steps 4-5, PRD §4.8, §6.8).

approve, in the caller's one write transaction:
1. export the three examples to <fixtures_dir>/promoted/ as redacted .eml + .expected.json (redact.py), while
   their content and cached readings still exist. Numbering continues past existing files; nothing is
   overwritten. The spec goes beside them (<name>.template.json) for measure-fixtures;
2. the template becomes active;
3. every cached model reading of the format is deleted: the Sources whose diagnostic carries the
   fingerprint, content kept or not. Routing is cache-first (§4.1), so without this the cache would keep
   serving the model's old reading and the promotion would accomplish nothing;
4. every retained email of the format is re-read now, by the user's own pipeline run, with no model:
   at tier 1 its transaction is updated in place (edits stay) and its content goes; one the template
   declines waits as pending_llm for the next sync, and is not charged as a failure of the template just
   approved. A re-read whose amount, date or account differs from
   the old reading sends the transaction to review (PRD §4.8 "Promotion re-tiers history");
5. the sighting becomes promoted, and a re-sync notice names the format's date range (slice 24): emails of it
   whose content is gone are read by the template only when re-synced.

Only a 'validated' candidate (replay passed) can be approved. reject turns a waiting candidate into a
discarded one (status 'proposed', passed: false) and the sighting back to accumulating. The row stays, as
for a failed replay, so the same three emails are not proposed on again: the next example rotates in first.
"""

import json
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hisaab.config import Settings
from hisaab.llm import LLMUnavailable
from hisaab.models import ExtractionCache, FormatSighting, ParserTemplate, Source, User, utc_now
from hisaab.parsing.email_interpreter import EmailArtifact
from hisaab.parsing.notices import write_notice
from hisaab.pipeline.derive import apply
from hisaab.pipeline.routing import Routed, route
from hisaab.promotion.redact import redact
from hisaab.promotion.sightings import with_fingerprint

RETIER_REASON: str = "read differently after its format got a template"
CRITERION: tuple[str, ...] = ("amount", "date", "account_identifier")  # PRD §9's "correct amount, date and account"
EXPORT_DIR: str = "promoted"


class NotFound(LookupError):
    pass


class NotApprovable(ValueError):
    pass


@dataclass(frozen=True)
class ApproveResult:
    reread: int  # emails of the format now read by the template
    waiting: int  # emails the template declined: back to the model on the next sync
    invalidated: int  # cached model readings deleted
    exported: list[str]  # fixture files written, relative to fixtures_dir
    flagged: int  # transactions sent to review because the template read them differently


def approve(session: Session, template_id: str, settings: Settings, now: datetime | None = None) -> ApproveResult:
    template: ParserTemplate = _get(session, template_id)
    if template.status != "validated":
        raise NotApprovable("Only a template that passed replay can be approved.")
    sighting: FormatSighting | None = session.scalars(
        select(FormatSighting).where(FormatSighting.template_id == template.id)).first()
    if sighting is None:
        raise NotApprovable("This template's format is no longer being tracked.")
    exported: list[str] = _export(session, template, settings.fixtures_dir)
    template.status, template.promoted_at = "active", utc_now(now)
    template.consecutive_failures, template.failure_window_started_at = 0, None

    members: list[Source] = list(session.scalars(select(Source).where(with_fingerprint(sighting.fingerprint))
                                                 .order_by(Source.ingest_seq)))
    readings: dict[str, dict[str, Any]] = {}  # content hash -> the model's old reading
    for content_hash in dict.fromkeys(s.content_hash for s in members):  # one cache row per hash
        cached: ExtractionCache | None = session.get(ExtractionCache, content_hash)
        if cached is not None:
            readings[content_hash] = json.loads(cached.extracted) if cached.extracted else {}
            session.delete(cached)  # through the session: routing below must not find it in the identity map
    session.flush()
    invalidated: int = len(readings)
    old: dict[str, dict[str, Any]] = {s.id: readings[s.content_hash] for s in members if readings.get(s.content_hash)}

    by_user: defaultdict[str, list[tuple[str, Routed]]] = defaultdict(list)
    for source in members:
        if source.content is not None and source.kind == "email":
            by_user[source.user_id].append((source.id, _reread(session, source, old.get(source.id))))
    for user_id, batch in by_user.items():
        apply(session, user_id, batch)
    sighting.state = "promoted"
    # the format's emails whose cached readings went: a re-sync reads them with the template (tech-design §5.2)
    write_notice(session, template, "approved", [m.received_at for m in members])
    session.flush()
    routed: list[Routed] = [r for batch in by_user.values() for _, r in batch]
    return ApproveResult(reread=sum(r.diagnostic["route"] == "tier1" for r in routed),
                         waiting=sum(r.state == "pending_llm" for r in routed),
                         invalidated=invalidated, exported=exported, flagged=sum(bool(r.review) for r in routed))


def reject(session: Session, template_id: str) -> None:
    template: ParserTemplate = _get(session, template_id)
    if template.status not in ("proposed", "validated") or template.origin != "llm_proposed":
        raise NotApprovable("Only a template waiting for review can be rejected.")
    validation: dict[str, Any] = json.loads(template.validation_result or "{}")
    template.status = "proposed"
    template.validation_result = json.dumps({**validation, "passed": False, "rejected": True,
                                             "problems": [*validation.get("problems", []), "Rejected by an admin"]},
                                            ensure_ascii=False)
    for sighting in session.scalars(select(FormatSighting).where(FormatSighting.template_id == template.id)):
        sighting.state = "accumulating"
    session.flush()


def _get(session: Session, template_id: str) -> ParserTemplate:
    template: ParserTemplate | None = session.get(ParserTemplate, template_id)
    if template is None or template.artifact_type != "email":
        raise NotFound("No such template.")
    return template


def _no_model(artifact: EmailArtifact) -> dict[str, Any]:
    raise LLMUnavailable("not asked during approval: read on the next sync")


def _reread(session: Session, source: Source, old: dict[str, Any] | None) -> Routed:
    # interpretation=None: the template was proven on its examples a moment ago, so an older email it declines
    # here is no failure streak (record_outcome would demote it inside its own approval)
    routed: Routed = replace(route(session, source, _no_model, ""), interpretation=None)
    if routed.diagnostic["route"] != "tier1" or routed.extraction is None or old is None:
        return routed
    differs: list[str] = [k for k in CRITERION if routed.extraction.get(k) != old.get(k)]
    return replace(routed, review=(f"{RETIER_REASON}: {', '.join(differs)}",)) if differs else routed


def _export(session: Session, template: ParserTemplate, fixtures_dir: Path) -> list[str]:
    folder: Path = fixtures_dir / EXPORT_DIR
    written: list[str] = []
    n: int = 1
    for source_id in json.loads(template.example_source_ids or "[]"):
        source: Source | None = session.get(Source, source_id)
        cached: ExtractionCache | None = session.get(ExtractionCache, source.content_hash) if source else None
        if source is None or source.content is None or cached is None or not cached.extracted:
            continue
        owner: User | None = session.get(User, source.user_id)
        eml, expected = redact(source.content, json.loads(cached.extracted), [owner.display_name] if owner else [])
        folder.mkdir(parents=True, exist_ok=True)
        while (folder / f"{template.name}_{n:02d}.eml").exists() or \
                (folder / f"{template.name}_{n:02d}.expected.json").exists():
            n += 1
        stem: Path = folder / f"{template.name}_{n:02d}"
        with open(stem.with_suffix(".eml"), "xb") as out:
            out.write(eml)
        with open(stem.with_suffix(".expected.json"), "x") as text:
            text.write(json.dumps(expected, indent=2, ensure_ascii=False) + "\n")
        written.append(f"{EXPORT_DIR}/{stem.name}.eml")
    if written:  # the spec beside its fixtures, so measure-fixtures reads them with it (parsing/measure.py)
        spec_file: Path = folder / f"{template.name}.template.json"
        spec_file.write_text(json.dumps(json.loads(template.spec), indent=2, ensure_ascii=False) + "\n")
    return written
