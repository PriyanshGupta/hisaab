"""Scheduled work (tech-design §6.7): APScheduler in this process.

A trigger only calls JobQueue.enqueue, the same call an HTTP handler makes:
scheduled and manual jobs share one code path. Interval triggers that fire at
startup and then every N hours, rather than fixed clock times, because the
machine sleeps: a laptop that is never awake at 03:00 would never clean up.
misfire_grace_time=None with coalesce runs an overdue job once, late, instead
of skipping it.

Later slices add: sync every 6 hours (13), the weekly sweep, daily backup.
"""

from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from hisaab.jobs.queue import JobQueue

CLEANUP_EVERY_HOURS: int = 24


def start_scheduler(queue: JobQueue) -> BackgroundScheduler:
    scheduler: BackgroundScheduler = BackgroundScheduler(
        job_defaults={"misfire_grace_time": None, "coalesce": True, "max_instances": 1})
    scheduler.add_job(queue.enqueue, "interval", args=["cleanup"], id="cleanup", hours=CLEANUP_EVERY_HOURS,
                      next_run_time=datetime.now().astimezone())
    scheduler.start()
    return scheduler
