"""The job queue (tech-design §6.7): one worker, FIFO, failures and timeouts contained."""

import json
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import Connection, func, select, text

from hisaab.db import Database
from hisaab.jobs.queue import INTERRUPTED, JobContext, JobKind, JobQueue, UnknownJobKind
from hisaab.models import Job, Payee
from tests import factories as f

# A statement that runs for many seconds unless interrupted.
LONG_QUERY: str = ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 2000000000) "
                   "SELECT count(*) FROM c")


def kind(run: Callable[[JobContext], dict[str, Any] | None], timeout: float = 30.0, joins: bool = False) -> JobKind:
    return JobKind(run=run, timeout=timeout, joins=joins)


@pytest.fixture
def make_queue(db: Database) -> Iterator[Callable[[dict[str, JobKind]], JobQueue]]:
    queues: list[JobQueue] = []

    def make(kinds: dict[str, JobKind]) -> JobQueue:
        queue: JobQueue = JobQueue(db, kinds)
        queues.append(queue)
        return queue

    yield make
    for queue in queues:
        queue.stop()


def wait_finished(db: Database, job_id: str, timeout: float = 10.0) -> Job:
    deadline: float = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with db.read_session() as s:
            job: Job | None = s.get(Job, job_id)
            if job is not None and job.state not in ("queued", "running"):
                s.expunge(job)  # keep its loaded values past the session's closing rollback
                return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


def error_message(job: Job) -> str:
    assert job.error is not None
    message: str = json.loads(job.error)["message"]
    return message


def payee_count(db: Database) -> int:
    with db.read_session() as s:
        count: int = s.scalar(select(func.count()).select_from(Payee)) or 0
        return count


def test_two_jobs_run_one_after_the_other(db: Database, make_queue: Callable[[dict[str, JobKind]], JobQueue]) -> None:
    spans: list[tuple[str, float, float]] = []

    def slow(ctx: JobContext) -> None:
        started: float = time.monotonic()
        time.sleep(0.2)
        spans.append((ctx.params["name"], started, time.monotonic()))

    queue: JobQueue = make_queue({"sweep": kind(slow)})
    queue.start()
    first: str = queue.enqueue("sweep", params={"name": "first"})
    second: str = queue.enqueue("sweep", params={"name": "second"})
    assert wait_finished(db, first).state == "succeeded"
    assert wait_finished(db, second).state == "succeeded"
    assert [name for name, _, _ in spans] == ["first", "second"]  # FIFO
    assert spans[0][2] <= spans[1][1]  # the second started only after the first ended


def test_result_and_timestamps_are_recorded(db: Database, make_queue: Callable[[dict[str, JobKind]], JobQueue]) -> None:
    queue: JobQueue = make_queue({"sweep": kind(lambda ctx: {"new": 3})})
    queue.start()
    job: Job = wait_finished(db, queue.enqueue("sweep"))
    assert (job.state, json.loads(job.result or "null"), job.error) == ("succeeded", {"new": 3}, None)
    assert job.started_at is not None and job.finished_at is not None and job.started_at <= job.finished_at


def test_a_job_that_throws_is_rolled_back_and_the_worker_carries_on(
    db: Database, make_queue: Callable[[dict[str, JobKind]], JobQueue],
) -> None:
    def fails(ctx: JobContext) -> None:
        with ctx.write() as s:
            s.add(Payee(normalized_name="Half written", type="merchant"))
            s.flush()
            raise ValueError("bank said no")

    queue: JobQueue = make_queue({"sweep": kind(fails), "cleanup": kind(lambda ctx: None)})
    queue.start()
    failed: Job = wait_finished(db, queue.enqueue("sweep"))
    after: Job = wait_finished(db, queue.enqueue("cleanup"))
    assert (failed.state, error_message(failed)) == ("failed", "bank said no")
    assert payee_count(db) == 0
    assert after.state == "succeeded"


def test_a_job_past_its_timeout_is_cancelled_at_a_checkpoint(
    db: Database, make_queue: Callable[[dict[str, JobKind]], JobQueue],
) -> None:
    def spins(ctx: JobContext) -> None:
        while True:
            ctx.checkpoint()
            time.sleep(0.02)

    queue: JobQueue = make_queue({"sweep": kind(spins, timeout=0.3), "cleanup": kind(lambda ctx: None)})
    queue.start()
    hung: Job = wait_finished(db, queue.enqueue("sweep"))
    assert (hung.state, error_message(hung)) == ("failed", "timed out after 0.3 s")
    assert wait_finished(db, queue.enqueue("cleanup")).state == "succeeded"  # the queue did not stall


def test_a_timeout_interrupts_a_running_sql_statement_and_rolls_back(
    db: Database, make_queue: Callable[[dict[str, JobKind]], JobQueue],
) -> None:
    def long_write(ctx: JobContext) -> None:
        with ctx.write() as s:
            s.add(Payee(normalized_name="Half written", type="merchant"))
            s.flush()
            s.execute(text(LONG_QUERY))

    queue: JobQueue = make_queue({"sweep": kind(long_write, timeout=0.3)})
    queue.start()
    started: float = time.monotonic()
    job: Job = wait_finished(db, queue.enqueue("sweep"))
    assert time.monotonic() - started < 5  # interrupted, not run to completion
    assert (job.state, error_message(job)) == ("failed", "timed out after 0.3 s")
    assert payee_count(db) == 0


def test_a_joining_kind_returns_the_queued_job_for_the_same_user(
    db: Database, conn: Connection, make_queue: Callable[[dict[str, JobKind]], JobQueue],
) -> None:
    asha: str = f.user(conn)["id"]
    ravi: str = f.user(conn)["id"]
    conn.commit()
    queue: JobQueue = make_queue({"sync": kind(lambda ctx: None, joins=True), "statement_merge": kind(lambda ctx: None)})
    # worker not started, so the first job stays queued
    first: str = queue.enqueue("sync", user_id=asha)
    assert queue.enqueue("sync", user_id=asha) == first
    assert queue.enqueue("sync", user_id=ravi) != first
    assert queue.enqueue("statement_merge", user_id=asha) != queue.enqueue("statement_merge", user_id=asha)
    queue.start()
    wait_finished(db, first)
    assert queue.enqueue("sync", user_id=asha) != first  # nothing to join once it has finished


def test_a_joining_kind_joins_a_running_job(db: Database, make_queue: Callable[[dict[str, JobKind]], JobQueue]) -> None:
    release: threading.Event = threading.Event()

    def waits(ctx: JobContext) -> None:
        release.wait(5)

    queue: JobQueue = make_queue({"cleanup": kind(waits, joins=True)})
    queue.start()
    first: str = queue.enqueue("cleanup")
    deadline: float = time.monotonic() + 5
    while time.monotonic() < deadline:
        with db.read_session() as s:
            if s.get(Job, first).state == "running":  # type: ignore[union-attr]
                break
        time.sleep(0.02)
    assert queue.enqueue("cleanup") == first
    release.set()
    assert wait_finished(db, first).state == "succeeded"


def test_a_kind_without_a_handler_is_refused(db: Database, make_queue: Callable[[dict[str, JobKind]], JobQueue]) -> None:
    queue: JobQueue = make_queue({})
    with pytest.raises(UnknownJobKind, match="sync"):
        queue.enqueue("sync")
    with db.read_session() as s:
        assert s.scalars(select(Job)).all() == []


def test_recover_fails_interrupted_jobs_and_runs_queued_ones(
    db: Database, conn: Connection, make_queue: Callable[[dict[str, JobKind]], JobQueue],
) -> None:
    f.job(conn, id="was-running", kind="sweep", state="running", started_at="2026-10-11T01:00:00.000Z",
          created_at="2026-10-11T00:59:00.000Z")
    f.job(conn, id="was-queued-2", kind="sweep", state="queued", created_at="2026-10-11T01:02:00.000Z")
    f.job(conn, id="was-queued-1", kind="sweep", state="queued", created_at="2026-10-11T01:01:00.000Z")
    f.job(conn, id="done", kind="sweep", state="succeeded", created_at="2026-10-11T00:00:00.000Z")
    conn.commit()
    ran: list[str] = []
    queue: JobQueue = make_queue({"sweep": kind(lambda ctx: ran.append(ctx.job_id))})
    queue.recover()
    interrupted: Job = wait_finished(db, "was-running", timeout=0.1)
    assert (interrupted.state, error_message(interrupted)) == ("failed", INTERRUPTED)
    assert interrupted.finished_at is not None
    queue.start()
    assert wait_finished(db, "was-queued-2").state == "succeeded"
    assert ran == ["was-queued-1", "was-queued-2"]  # in created_at order
    with db.read_session() as s:
        assert s.get(Job, "done").state == "succeeded"  # type: ignore[union-attr]
