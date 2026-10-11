"""Every job kind the queue can run, with its timeout (tech-design §6.7).

Kinds join their slices' handlers here: backfill (30 min, slice 16), sweep,
resync, statement_merge, reprocess_normalization.
"""

from hisaab.config import Settings
from hisaab.ingest.sync import make_sync
from hisaab.jobs import cleanup
from hisaab.jobs.queue import JobKind


def job_kinds(settings: Settings) -> dict[str, JobKind]:
    return {
        "cleanup": JobKind(run=cleanup.run, timeout=10 * 60, joins=True),
        "sync": JobKind(run=make_sync(settings), timeout=10 * 60, joins=True),  # PRD §10.2: one sync per user
    }
