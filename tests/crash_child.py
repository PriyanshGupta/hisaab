"""Child process for the crash test (tests/test_job_verification.py), killed mid-job.

    python -m tests.crash_child DB_PATH USER_ID

Runs a sync-shaped job: batch 1 commits, batch 2 is mid-transaction when the
parent sends SIGKILL, and the sync clock would only advance at the very end,
as tech-design §5.1 step 5 requires. Prints MID-JOB once batch 2 has rows
flushed (written to SQLite, uncommitted).
"""

import sys
import time
from pathlib import Path

from hisaab.db import Database
from hisaab.jobs.queue import JobContext, JobKind, JobQueue
from hisaab.models import GmailConnection, Payee, utc_now

BATCH: int = 10


def slow_sync(ctx: JobContext) -> dict[str, int]:
    for batch in (1, 2):
        with ctx.write() as s:
            for i in range(BATCH):
                s.add(Payee(normalized_name=f"Synced batch {batch} #{i}", type="merchant"))
                s.flush()
                if batch == 2 and i == BATCH // 2:
                    print("MID-JOB", flush=True)
                    time.sleep(60)  # the parent kills us here
    with ctx.write() as s:
        connection: GmailConnection | None = s.get(GmailConnection, ctx.user_id)
        assert connection is not None
        connection.last_successful_sync_at = utc_now()
    return {"new": 2 * BATCH}


def main() -> None:
    db_path, user_id = Path(sys.argv[1]), sys.argv[2]
    queue: JobQueue = JobQueue(Database(db_path), {"sync": JobKind(run=slow_sync, timeout=600)})
    queue.recover()
    queue.start()
    queue.enqueue("sync", user_id=user_id)
    time.sleep(120)


if __name__ == "__main__":
    main()
