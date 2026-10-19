"""Every job kind the queue can run, with its timeout (tech-design §6.7).

A backfill is a sync whose window reaches back backfill_window_months (slice
16); its own kind only for the longer timeout. Kinds join their slices'
handlers here: sweep, resync, reprocess_normalization.
"""

from hisaab.config import Settings
from hisaab.ingest.sync import make_sync
from hisaab.jobs import cleanup
from hisaab.jobs.queue import JobKind
from hisaab.statements import merge


def job_kinds(settings: Settings) -> dict[str, JobKind]:
    return {
        "cleanup": JobKind(run=cleanup.run, timeout=10 * 60, joins=True),
        "sync": JobKind(run=make_sync(settings), timeout=10 * 60, joins=True),  # PRD §10.2: one sync per user
        "backfill": JobKind(run=make_sync(settings), timeout=30 * 60, joins=True),
        "statement_merge": JobKind(run=merge.run, timeout=5 * 60),  # one write transaction (§7 step 6)
    }
