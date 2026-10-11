"""APScheduler and the app's job wiring (tech-design §6.7)."""

import json
import time
from datetime import timedelta
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Connection, select

from hisaab.config import Settings
from hisaab.db import Database
from hisaab.jobs.kinds import job_kinds
from hisaab.jobs.queue import INTERRUPTED, JobQueue
from hisaab.jobs.scheduler import start_scheduler
from hisaab.models import Job
from tests import factories as f


def jobs_of_kind(db: Database, kind: str) -> list[tuple[str, str]]:
    with db.read_session() as s:
        return [(j.id, j.state) for j in s.scalars(select(Job).where(Job.kind == kind))]


def wait_until(check: Any, timeout: float = 5.0) -> None:
    deadline: float = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.02)


def test_cleanup_is_enqueued_at_start_then_every_24_hours(db: Database, settings: Settings) -> None:
    queue: JobQueue = JobQueue(db, job_kinds(settings))  # worker not started: what the scheduler enqueues stays queued
    scheduler: BackgroundScheduler = start_scheduler(queue)
    try:
        wait_until(lambda: jobs_of_kind(db, "cleanup"))
        job: Any = scheduler.get_job("cleanup")
        assert job.trigger.interval == timedelta(hours=24)
        # a sleeping laptop misses run times: run late (once) rather than skip
        assert (job.misfire_grace_time, job.coalesce) == (None, True)
        assert [state for _, state in jobs_of_kind(db, "cleanup")] == ["queued"]
    finally:
        scheduler.shutdown(wait=False)


def test_app_start_fails_interrupted_jobs_and_runs_cleanup(app: FastAPI, conn: Connection) -> None:
    f.job(conn, id="killed-mid-sync", kind="sync", state="running", created_at="2026-10-11T01:00:00.000Z")
    conn.commit()
    db: Database = app.state.db
    with TestClient(app):
        wait_until(lambda: [s for _, s in jobs_of_kind(db, "cleanup")] == ["succeeded"])
        with db.read_session() as s:
            killed: Job | None = s.get(Job, "killed-mid-sync")
            assert killed is not None and killed.state == "failed"
            assert json.loads(killed.error or "{}") == {"message": INTERRUPTED}


def test_syncs_are_enqueued_for_connected_users_at_start_then_every_6_hours(
    db: Database, settings: Settings, conn: Connection,
) -> None:
    connected: str = f.gmail_connection(conn, last_successful_sync_at="2026-10-11T00:00:00.000Z")["user_id"]
    f.gmail_connection(conn, status="needs_reauth")  # a dead grant is not synced; the banner asks to reconnect
    conn.commit()
    queue: JobQueue = JobQueue(db, job_kinds(settings))
    scheduler: BackgroundScheduler = start_scheduler(queue)
    try:
        wait_until(lambda: jobs_of_kind(db, "sync"))  # catch-up at startup (tech-design §6.7)
        assert scheduler.get_job("sync").trigger.interval == timedelta(hours=6)  # sync_interval_hours
        with db.read_session() as s:
            assert list(s.scalars(select(Job.user_id).where(Job.kind == "sync"))) == [connected]
    finally:
        scheduler.shutdown(wait=False)


def test_a_connected_user_whose_backfill_never_finished_gets_a_backfill_at_start(
    db: Database, settings: Settings, conn: Connection,
) -> None:
    """A process killed mid-backfill: the catch-up must not be a 48-hour sync that advances the clock."""
    interrupted: str = f.gmail_connection(conn)["user_id"]  # clock still NULL
    conn.commit()
    queue: JobQueue = JobQueue(db, job_kinds(settings))
    scheduler: BackgroundScheduler = start_scheduler(queue)
    try:
        wait_until(lambda: jobs_of_kind(db, "backfill"))
        with db.read_session() as s:
            assert list(s.scalars(select(Job.user_id).where(Job.kind.in_(("sync", "backfill"))))) == [interrupted]
    finally:
        scheduler.shutdown(wait=False)
