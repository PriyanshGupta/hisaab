"""Run a user's waiting Sources through tier routing and the pipeline, a batch at a time.

Waiting = pending (just stored, or re-fetched for a re-parse) or pending_llm
(the model was unreachable last time). The set is taken once at the start, so
Sources that stay pending_llm this run are not retried in a loop.

Each batch: route outside the writer lock (model calls take seconds), then one
write transaction for the pipeline. Once the model is found unreachable, the
rest of the run does not ask again: each would wait out the same retries.

Before the batches, retained model-read emails that were never counted are counted under their
format's fingerprint from their cached reading (slice 22). After them, formats that reached the
promotion threshold get their one template proposal (promotion/propose.py).
"""

from collections import Counter
from collections.abc import Callable
from typing import Any

from sqlalchemy import select

from hisaab.config import Settings
from hisaab.jobs.queue import JobContext
from hisaab.llm import LLMUnavailable, Transport, extract_email
from hisaab.models import Source
from hisaab.parsing.email_interpreter import EmailArtifact
from hisaab.pipeline.derive import PipelineCounts, apply
from hisaab.pipeline.routing import Routed, route
from hisaab.promotion.propose import propose_ready
from hisaab.promotion.sightings import sight_retained

BATCH: int = 25


def process_pending(ctx: JobContext, user_id: str, settings: Settings, transport: Transport,
                    sleep: Callable[[float], None]) -> dict[str, int]:
    down: list[LLMUnavailable] = []
    totals: Counter[str] = Counter()

    def extract(artifact: EmailArtifact) -> dict[str, Any]:
        if down:
            raise down[0]
        totals["llm_calls"] += 1
        try:
            return extract_email(settings, artifact, transport=transport, sleep=sleep)
        except LLMUnavailable as e:
            down.append(e)
            raise

    with ctx.write() as s:  # emails the model read before they were counted (slice 22): no model call
        totals["sighted"] += sight_retained(s, user_id)
    with ctx.read() as s:
        waiting: list[str] = list(s.scalars(select(Source.id).where(
            Source.user_id == user_id, Source.kind == "email", Source.content.is_not(None),
            Source.processing_state.in_(("pending", "pending_llm"))).order_by(Source.ingest_seq)))
    for start in range(0, len(waiting), BATCH):
        ctx.checkpoint()
        with ctx.read() as s:
            batch: list[tuple[str, Routed]] = [
                (i, route(s, s.get_one(Source, i), extract, settings.email_model)) for i in waiting[start:start + BATCH]]
        with ctx.write() as s:
            counts: PipelineCounts = apply(s, user_id, batch)
            ctx.report(s, {"read": start + len(batch), "to_read": len(waiting)})
        totals.update({k: v for k, v in vars(counts).items()})
        totals.update("route:" + r.diagnostic["route"] for _, r in batch)
    proposals: dict[str, int] = propose_ready(ctx, settings, transport, sleep)  # outside the writer lock
    return {"templates_proposed": proposals["proposed"], "tier1": totals["route:tier1"], "cache_hits": totals["route:cache"], "llm_calls": totals["llm_calls"],
            **{k: totals[k] for k in vars(PipelineCounts())}}
