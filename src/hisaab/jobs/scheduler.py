"""Scheduled work (tech-design §6.7): APScheduler in this process.

A trigger only calls JobQueue.enqueue, the same call an HTTP handler makes:
scheduled and manual jobs share one code path. Interval triggers that fire at
startup and then every N hours, rather than fixed clock times, because the
machine sleeps: a laptop that is never awake at 03:00 would never clean up.
misfire_grace_time=None with coalesce runs an overdue job once, late, instead
of skipping it.

The sync's startup run is the catch-up sync §6.7 asks for: its window reaches
back to each user's last successful sync. Later slices add the weekly sweep
and the daily backup.
"""

from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from hisaab.jobs.queue import JobQueue
from hisaab.models import GmailConnection
from hisaab.tunables import tunable

CLEANUP_EVERY_HOURS: int = 24


def enqueue_syncs(queue: JobQueue) -> None:
    """A sync for every user whose Gmail is connected (a needs_reauth grant would only fail)."""
    with queue.db.read_session() as s:
        users: list[str] = list(s.scalars(select(GmailConnection.user_id).where(GmailConnection.status == "connected")))
    for user_id in users:
        queue.enqueue("sync", user_id=user_id)


def start_scheduler(queue: JobQueue) -> BackgroundScheduler:
    with queue.db.read_session() as s:
        sync_hours: int = tunable(s, "sync_interval_hours")
    now: datetime = datetime.now().astimezone()
    scheduler: BackgroundScheduler = BackgroundScheduler(
        job_defaults={"misfire_grace_time": None, "coalesce": True, "max_instances": 1})
    scheduler.add_job(queue.enqueue, "interval", args=["cleanup"], id="cleanup", hours=CLEANUP_EVERY_HOURS,
                      next_run_time=now)
    scheduler.add_job(enqueue_syncs, "interval", args=[queue], id="sync", hours=sync_hours, next_run_time=now)
    scheduler.start()
    return scheduler
