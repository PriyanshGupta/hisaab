"""Every job kind the queue can run, with its timeout (tech-design §6.7).

Kinds join their slices' handlers here: sync (10 min, slice 13), backfill
(30 min, slice 16), sweep, resync, statement_merge, reprocess_normalization.
"""

from hisaab.jobs import cleanup
from hisaab.jobs.queue import JobKind


def job_kinds() -> dict[str, JobKind]:
    return {"cleanup": JobKind(run=cleanup.run, timeout=10 * 60, joins=True)}
