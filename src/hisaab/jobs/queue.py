"""The job queue (tech-design §6.7): only one thing writes at a time.

All background write work (sync, re-sync, statement merge, sweep, reprocess,
cleanup) is a jobs row plus an id on an in-memory FIFO, run one at a time by a
single worker thread. HTTP handlers and the scheduler both call enqueue(): one
code path. The row carries state and result, so a closed tab can come back and
see where things stand.

- A job that throws: its open write transaction rolls back, the row is marked
  failed with the message, and the worker moves on to the next job.
- A job past its kind's timeout: a watchdog sets the cancel flag (raised at the
  job's next checkpoint() or write()) and interrupts the SQL statement running
  on the write connection, so the transaction rolls back.
  shortcut: Python cannot stop a thread, so a job blocked outside SQLite (a
  network call with no timeout) holds the queue until that call returns. Every
  network client a job uses must set its own timeout; Gmail's does (slice 13).
- Process killed mid-job: SQLite discards the uncommitted transaction; recover()
  at startup marks rows still 'running' failed, and re-enqueues 'queued' rows,
  whose in-memory place in line died with the process.
- Joining kinds (PRD §10.2, one sync per user): enqueueing one while the same
  kind is queued or running for the same user returns the existing job id.
"""

import json
import logging
import queue
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from hisaab.db import Database
from hisaab.models import Job, utc_now

INTERRUPTED: str = "interrupted by shutdown"
WRITE_WAIT: float = 60.0  # how long a job waits for a user's short write to release the lock
INTERRUPT_REPEAT: float = 0.1  # re-interrupt until the job ends: a statement starting just after a miss is caught

_log: logging.Logger = logging.getLogger(__name__)


class UnknownJobKind(ValueError):
    pass


class JobTimeout(Exception):
    pass


@dataclass(frozen=True)
class JobKind:
    run: Callable[["JobContext"], dict[str, Any] | None]  # returns the result stored on the row
    timeout: float  # seconds
    joins: bool = False


class JobContext:
    """What a running job gets: its row's values, and the only way it writes."""

    def __init__(self, db: Database, job_id: str, user_id: str | None, params: dict[str, Any]) -> None:
        self.job_id: str = job_id
        self.user_id: str | None = user_id
        self.params: dict[str, Any] = params
        self.cancelled: threading.Event = threading.Event()
        self._db: Database = db

    def checkpoint(self) -> None:
        """Raise JobTimeout if the job has been cancelled. Long jobs call it between batches."""
        if self.cancelled.is_set():
            raise JobTimeout()

    def report(self, session: Session, progress: dict[str, int | None]) -> None:
        """Record progress on this job's row inside the caller's write transaction (no row, no-op)."""
        session.execute(update(Job).where(Job.id == self.job_id).values(progress=json.dumps(progress)))

    @contextmanager
    def read(self) -> Iterator[Session]:
        """A read-only session (the read pool): sees the last committed data, cannot write."""
        with self._db.read_session() as session:
            yield session

    @contextmanager
    def write(self) -> Iterator[Session]:
        """One write transaction on the write connection: commits on success, rolls back on error."""
        self.checkpoint()
        with self._db.write_session(timeout=WRITE_WAIT) as session:
            yield session


class JobQueue:
    def __init__(self, db: Database, kinds: dict[str, JobKind]) -> None:
        self.db: Database = db
        self.kinds: dict[str, JobKind] = kinds
        self._pending: queue.Queue[str | None] = queue.Queue()
        self._worker: threading.Thread | None = None

    def enqueue(self, kind: str, user_id: str | None = None, params: dict[str, Any] | None = None) -> str:
        """Create the job row and queue it; returns the job id (an existing one if this kind joins)."""
        spec: JobKind | None = self.kinds.get(kind)
        if spec is None:  # refuse rather than leave a row queued forever
            raise UnknownJobKind(f"no handler for job kind {kind!r}")
        with self.db.write_session() as s:  # under the writer lock, so check-then-insert cannot race
            if spec.joins:
                existing: str | None = s.scalars(
                    select(Job.id).where(Job.kind == kind, Job.user_id == user_id,
                                         Job.state.in_(("queued", "running"))).limit(1)).first()
                if existing is not None:
                    return existing
            job: Job = Job(kind=kind, user_id=user_id, state="queued",
                           params=None if params is None else json.dumps(params))
            s.add(job)
            s.flush()
            job_id: str = job.id
        self._pending.put(job_id)  # after commit, so the worker never looks for an uncommitted row
        return job_id

    def recover(self) -> None:
        """Startup, before start(): fail jobs a dead process left running; requeue queued ones."""
        with self.db.write_session(timeout=WRITE_WAIT) as s:
            for job in s.scalars(select(Job).where(Job.state == "running")):
                job.state, job.error, job.finished_at = "failed", json.dumps({"message": INTERRUPTED}), utc_now()
            queued: list[str] = list(s.scalars(
                select(Job.id).where(Job.state == "queued").order_by(Job.created_at, Job.id)))
        for job_id in queued:
            self._pending.put(job_id)

    def start(self) -> None:
        self._worker = threading.Thread(target=self._work, name="hisaab-job-worker", daemon=True)
        self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Let the current job finish (up to timeout); a job still running then dies with the
        process and recover() fails it on the next start."""
        if self._worker is not None:
            self._pending.put(None)
            self._worker.join(timeout)

    def _work(self) -> None:
        while (job_id := self._pending.get()) is not None:
            try:
                self._run(job_id)
            except Exception:  # bookkeeping failed (e.g. writer busy past WRITE_WAIT); keep serving
                _log.exception("job %s: could not record its state", job_id)

    def _run(self, job_id: str) -> None:
        with self.db.write_session(timeout=WRITE_WAIT) as s:
            job: Job | None = s.get(Job, job_id)
            if job is None or job.state != "queued":
                return
            job.state, job.started_at = "running", utc_now()
            kind, user_id, params = job.kind, job.user_id, json.loads(job.params or "{}")
        spec: JobKind | None = self.kinds.get(kind)
        ctx: JobContext = JobContext(self.db, job_id, user_id, params)
        done: threading.Event = threading.Event()
        state: str = "succeeded"
        result: dict[str, Any] | None = None
        error: str | None = None
        if spec is None:  # a queued row whose kind lost its handler between runs
            state, error = "failed", f"no handler for job kind {kind!r}"
        else:
            watchdog: threading.Thread = threading.Thread(
                target=self._watch, args=(ctx, done, spec.timeout, threading.get_ident()), daemon=True)
            watchdog.start()
            try:
                result = spec.run(ctx)
            except Exception as exc:
                state = "failed"
                error = f"timed out after {spec.timeout:g} s" if ctx.cancelled.is_set() else (
                    str(exc) or type(exc).__name__)
            finally:
                done.set()
        with self.db.write_session(timeout=WRITE_WAIT) as s:
            row: Job | None = s.get(Job, job_id)
            if row is not None:
                row.state, row.finished_at = state, utc_now()
                row.result = None if result is None else json.dumps(result)
                row.error = None if error is None else json.dumps({"message": error})

    def _watch(self, ctx: JobContext, done: threading.Event, timeout: float, owner: int) -> None:
        if done.wait(timeout):
            return
        ctx.cancelled.set()
        while True:
            self.db.interrupt_writer(owner)
            if done.wait(INTERRUPT_REPEAT):
                return
