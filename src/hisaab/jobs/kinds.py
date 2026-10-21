"""Every job kind the queue can run, with its timeout (tech-design §6.7).

A backfill is a sync whose window reaches back backfill_window_months (slice
16); its own kind only for the longer timeout. Kinds join their slices'
handlers here: reprocess_normalization.
"""

from hisaab.config import Settings
from hisaab.ingest.sweep import make_sweep
from hisaab.ingest.sync import make_sync
from hisaab.jobs import cleanup
from hisaab.jobs.queue import JobKind
from hisaab.ops.backup import make_backup
from hisaab.statements import merge


def job_kinds(settings: Settings) -> dict[str, JobKind]:
    return {
        "cleanup": JobKind(run=cleanup.run, timeout=10 * 60, joins=True),
        "sync": JobKind(run=make_sync(settings), timeout=10 * 60, joins=True),  # PRD §10.2: one sync per user
        "backfill": JobKind(run=make_sync(settings), timeout=30 * 60, joins=True),
        # an approved sender's mail for one inbox (params {"senders": [...]}): never joins another job
        "sender_backfill": JobKind(run=make_sync(settings), timeout=30 * 60),
        # an admin's re-sync of a date range for one inbox (params {"from", "to"}), tech-design §5.2. Never joins:
        # joining ignores params, so a second range would be swallowed; the queue runs one job at a time anyway
        "resync": JobKind(run=make_sync(settings), timeout=30 * 60),
        "backup": JobKind(run=make_backup(settings), timeout=5 * 60, joins=True),  # daily (tech-design §9.2)
        "statement_merge": JobKind(run=merge.run, timeout=5 * 60),  # one write transaction (§7 step 6)
        "sweep": JobKind(run=make_sweep(settings), timeout=10 * 60, joins=True),  # one per inbox (§5.3)
    }
