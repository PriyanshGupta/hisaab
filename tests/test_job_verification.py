"""Build-plan slice 12 verification, through the real app and a real process kill.

1. A report read while a long job holds the write transaction is not blocked,
   and shows the pre-job data (tech-design §2.2: the reason for SQLite in WAL).
2. A read handler cannot write: the session it holds is on the read-only pool.
3. Kill the process mid-job and restart: the job is failed, nothing is half
   written, and the sync clock never advanced.
"""

import json
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Connection, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from hisaab.config import PROJECT_ROOT, Settings
from hisaab.db import Database
from hisaab.jobs.queue import INTERRUPTED, JobContext, JobKind
from hisaab.main import create_app
from hisaab.models import GmailConnection, Job, Payee
from hisaab.transactions import repository as txns
from tests import factories as f
from tests.conftest import login, make_user

JOB_HOLDS_WRITE_FOR: float = 2.0


@pytest.fixture
def asha(app: FastAPI, client: TestClient) -> tuple[TestClient, str, str]:
    user_id: str = make_user(app, "asha", "asha password 1")
    login(client, "asha", "asha password 1")
    wallet: str = client.post("/api/accounts", json={
        "type": "cash", "display_name": "Wallet", "opening_balance_paise": 0,
        "opening_balance_date": "2026-07-01"}).json()["id"]
    return client, user_id, wallet


def wait_finished(db: Database, job_id: str, timeout: float = 10.0) -> str:
    deadline: float = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with db.read_session() as s:
            state: str | None = s.scalar(select(Job.state).where(Job.id == job_id))
        if state not in ("queued", "running"):
            assert state is not None
            return state
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_a_report_read_is_not_blocked_by_a_long_job(app: FastAPI, asha: tuple[TestClient, str, str]) -> None:
    client, user_id, wallet = asha
    client.post("/api/transactions", json={"kind": "expense", "amount_paise": 10000,
                                           "transaction_date": "2026-10-05", "from_account_id": wallet,
                                           "payee": "Before the job"})
    writing: threading.Event = threading.Event()

    def long_job(ctx: JobContext) -> None:
        with ctx.write() as s:
            txns.create_manual(s, user_id, kind="expense", amount=20000, transaction_date="2026-10-06",
                               from_account_id=wallet, payee_raw="Written by the job")
            s.flush()  # SQLite's write lock is now held, uncommitted rows in place
            writing.set()
            time.sleep(JOB_HOLDS_WRITE_FOR)

    app.state.jobs.kinds["sweep"] = JobKind(run=long_job, timeout=30)
    job_id: str = app.state.jobs.enqueue("sweep")
    assert writing.wait(5)
    started: float = time.perf_counter()
    during: list[dict[str, Any]] = client.get("/api/transactions").json()
    elapsed: float = time.perf_counter() - started
    print(f"\nGET /api/transactions while the job held the write lock: {elapsed * 1000:.0f} ms, "
          f"{len(during)} transaction(s): {[t['payee_raw'] for t in during]}")
    assert elapsed < 0.5  # blocked would mean waiting out the job (2 s) or busy_timeout (5 s)
    assert [t["payee_raw"] for t in during] == ["Before the job"]  # the last committed snapshot
    assert wait_finished(app.state.db, job_id) == "succeeded"
    after: list[dict[str, Any]] = client.get("/api/transactions").json()
    print(f"after the job committed: {len(after)} transaction(s)")
    assert sorted(t["payee_raw"] for t in after) == ["Before the job", "Written by the job"]


def test_a_read_handler_cannot_write(asha: tuple[TestClient, str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = asha
    real_list: Callable[..., Any] = txns.list_transactions

    def list_and_sneak_a_write(session: Session, user_id: str, **kw: Any) -> Any:
        session.add(Payee(normalized_name="Sneaky", type="merchant"))
        session.flush()  # the handler's own session, from db.read_session()
        return real_list(session, user_id, **kw)

    monkeypatch.setattr(txns, "list_transactions", list_and_sneak_a_write)
    with pytest.raises(OperationalError, match="attempt to write a readonly database"):
        client.get("/api/transactions")


def test_kill_mid_job_then_restart(settings: Settings, conn: Connection) -> None:
    clock: str = "2026-10-10T06:00:00.000Z"
    user_id: str = f.gmail_connection(conn, last_successful_sync_at=clock)["user_id"]
    conn.commit()
    child: subprocess.Popen[str] = subprocess.Popen(
        [sys.executable, "-m", "tests.crash_child", str(settings.database_path), user_id],
        cwd=PROJECT_ROOT, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "MID-JOB"
        child.send_signal(signal.SIGKILL)
        child.wait(10)
    finally:
        child.kill()
    with TestClient(create_app(settings)) as restarted:  # startup recovery runs in the lifespan
        db: Database = restarted.app.state.db  # type: ignore[attr-defined]
        with db.read_session() as s:
            job: Job = s.scalars(select(Job).where(Job.kind == "sync")).one()
            batch_1: int = s.scalar(select(func.count()).where(Payee.normalized_name.like("Synced batch 1 %"))) or 0
            batch_2: int = s.scalar(select(func.count()).where(Payee.normalized_name.like("Synced batch 2 %"))) or 0
            synced_at: str | None = s.scalar(select(GmailConnection.last_successful_sync_at))
            print(f"\nafter SIGKILL and restart: job {job.state} ({json.loads(job.error or '{}')['message']}), "
                  f"batch 1 rows {batch_1}, batch 2 rows {batch_2}, sync clock {synced_at}")
            assert (job.state, json.loads(job.error or "{}")) == ("failed", {"message": INTERRUPTED})
    assert (batch_1, batch_2) == (10, 0)  # the committed batch is whole; the killed one left nothing
    assert synced_at == clock  # never advanced
