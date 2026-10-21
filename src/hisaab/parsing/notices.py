"""Parser-change notices (tech-design §5.2): "promoting or editing a template writes a pending re-sync notice —
which template, when, and the affected date range — surfaced as a banner on the admin screen". A prompt, never
an automatic re-sync (§11: re-sync costs quota and rewrites data).

The affected range is the India dates of the first and last emails the template read: Sources whose
diagnostic names it on route tier1. Approval passes the received times itself, since the format's emails that
need re-reading still carry the model's diagnostic. A template that read nothing has no history to fix, so it
gets no notice. Starting a re-sync that covers a notice resolves it (ingest.resync.resolve_notices).
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hisaab.clock import LOCAL_TZ
from hisaab.models import ParserTemplate, ResyncNotice, Source


def write_notice(session: Session, template: ParserTemplate, reason: str,
                 received: list[str] | None = None) -> ResyncNotice | None:
    """reason: edited | approved | shipped_updated. received: UTC times of the affected emails, if not the ones
    whose diagnostic names this template."""
    times: list[str] = received if received is not None else list(session.scalars(select(Source.received_at).where(
        func.json_extract(Source.diagnostic, "$.route") == "tier1",
        func.json_extract(Source.diagnostic, "$.template") == template.name)))
    if not times:
        return None
    days: list[str] = sorted(_india_day(t) for t in times)
    notice: ResyncNotice = ResyncNotice(template_id=template.id, template_name=template.name, reason=reason,
                                        range_from=days[0], range_to=days[-1])
    session.add(notice)
    session.flush()
    return notice


def _india_day(utc_text: str) -> str:
    return datetime.fromisoformat(utc_text).astimezone(LOCAL_TZ).date().isoformat()
