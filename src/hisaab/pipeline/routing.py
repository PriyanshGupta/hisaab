"""Tier routing (tech-design §4.1): one function decides how each email Source is read.

1. extraction_cache by content hash: a hit is the answer, with no template run and
   no model call. That is what makes re-sync free and deterministic.
2. Tier 1: active templates for this sender and subject, gated on critical fields.
3. Tier 2: the LLM. The result is cached for step 1 next time.

Read-only: route() runs outside the writer lock, because a model call takes
seconds. The pipeline (derive.apply) writes what it decided in one transaction.

Only tier-2 results are cached. A tier-1 result is free to recompute, and
caching it would hide a template fix from re-sync.

Retention (§3.7) is decided here: a tier-1 match discards its content; anything
read by the model keeps it (its format awaits promotion, slice 22), as do
pending_llm and failed Sources. A model-read email carries its fingerprint (slice 22) in the
diagnostic and in Routed, for the sighting count; a format already excluded as non-transactional
is answered without the model. The diagnostic never holds the email's own
text or extracted values: it outlives the content as the only forensic trail.
It does name the sender's address: dedup's fallback never merges two emails
from one sender (slice 15), and that has to work after the content is gone.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeAlias

from sqlalchemy.orm import Session

from hisaab.llm import ContractError, LLMUnavailable
from hisaab.models import ExtractionCache, Source
from hisaab.parsing.email_interpreter import EmailArtifact, EmailTemplate, Interpretation, interpret, parse_eml
from hisaab.parsing.email_spec import CONTRACT_FIELDS
from hisaab.parsing.fingerprint import Fingerprint, fingerprint
from hisaab.parsing.templates import email_candidates
from hisaab.promotion.sightings import excluded

# One email in, the §10.4 contract out; raises LLMUnavailable or ContractError.
LlmExtract: TypeAlias = Callable[[EmailArtifact], dict[str, Any]]

STATE_FOR_OUTCOME: dict[str, str] = {"transaction": "parsed", "non_transactional": "non_transactional",
                                     "unparseable": "failed"}


@dataclass(frozen=True)
class Routed:
    extraction: dict[str, Any] | None  # the contract; None when nothing could read it yet
    state: str  # the Source's processing_state
    discard_content: bool
    diagnostic: dict[str, Any]
    cache_entry: ExtractionCache | None  # a new cache row to store
    interpretation: Interpretation | None  # tier-1 attempts, for the template failure counters
    fingerprint: Fingerprint | None = None  # tier 2 only: the format, for the sighting count (§4.9)
    review: tuple[str, ...] = ()  # extra review reasons (a re-read after promotion that disagrees)


def _attempts(interpretation: Interpretation) -> list[dict[str, Any]]:
    return [{"template": a.template_name, "failed": [f"{f.field}: {f.reason}" for f in a.failures]}
            for a in interpretation.attempts]


def route(session: Session, source: Source, llm: LlmExtract, model: str) -> Routed:
    assert source.content is not None, "only Sources holding content can be routed"
    artifact: EmailArtifact = parse_eml(source.content)
    routed: Routed = _route(session, source, artifact, llm, model)
    routed.diagnostic["sender"] = artifact.sender
    return routed


def _route(session: Session, source: Source, artifact: EmailArtifact, llm: LlmExtract, model: str) -> Routed:
    cached: ExtractionCache | None = session.get(ExtractionCache, source.content_hash)
    if cached is not None:
        extraction: dict[str, Any] = (json.loads(cached.extracted) if cached.extracted else
                                      {"outcome": cached.outcome, **dict.fromkeys(CONTRACT_FIELDS), "missing_fields": []})
        # a model reading keeps its format's fingerprint: a re-sync must neither recount the email nor hide it
        # from promotion's cache invalidation (§4.1)
        fp_cached: Fingerprint | None = fingerprint(artifact) if cached.tier == 2 else None
        return Routed(extraction, STATE_FOR_OUTCOME[cached.outcome], cached.tier == 1,
                      {"route": "cache", "tier": cached.tier, "model": cached.model, "outcome": cached.outcome,
                       "missing_fields": extraction["missing_fields"],
                       **({"fingerprint": fp_cached.value} if fp_cached else {})}, None, None, fp_cached)

    candidates: list[EmailTemplate] = email_candidates(session, artifact.sender, artifact.subject)
    interpretation: Interpretation = interpret(candidates, artifact)
    if interpretation.extraction is not None:
        winner: str = interpretation.attempts[-1].template_name
        found: dict[str, Any] = interpretation.extraction
        return Routed(found, STATE_FOR_OUTCOME[found["outcome"]], True,
                      {"route": "tier1", "template": winner, "outcome": found["outcome"],
                       "missing_fields": found["missing_fields"], "attempts": _attempts(interpretation)},
                      None, interpretation)

    tried: list[dict[str, Any]] = _attempts(interpretation)
    counted: Interpretation | None = interpretation if interpretation.attempts else None
    fp: Fingerprint = fingerprint(artifact)
    if excluded(session, fp.value):  # a known non-transactional format: no model call (ruling 2026-10-10)
        notice: dict[str, Any] = {"outcome": "non_transactional", **dict.fromkeys(CONTRACT_FIELDS),
                                  "missing_fields": []}
        return Routed(notice, "non_transactional", True, {"route": "excluded", "fingerprint": fp.value,
                                                          "outcome": "non_transactional", "missing_fields": [],
                                                          "tier1_attempts": tried}, None, counted, fp)
    try:
        contract: dict[str, Any] = llm(artifact)
    except LLMUnavailable as e:
        return Routed(None, "pending_llm", False, {"route": "pending_llm", "reason": str(e), "tier1_attempts": tried,
                                                   "fingerprint": fp.value}, None, counted)
    except ContractError as e:  # the field name only: the message quotes the model's value
        return Routed(None, "failed", False, {"route": "tier2", "model": model, "contract_violation":
                                              str(e).split(":", 1)[0], "tier1_attempts": tried,
                                              "fingerprint": fp.value}, None, counted)
    entry: ExtractionCache = ExtractionCache(content_hash=source.content_hash, outcome=contract["outcome"],
                                             extracted=json.dumps(contract), model=model, tier=2)
    return Routed(contract, STATE_FOR_OUTCOME[contract["outcome"]], False,
                  {"route": "tier2", "model": model, "outcome": contract["outcome"], "fingerprint": fp.value,
                   "missing_fields": contract["missing_fields"], "tier1_attempts": tried}, entry, counted, fp)
