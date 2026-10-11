"""The single writer lock (write-path ruling, 2026-10-10) and the two pools (tech-design §6.7)."""

import threading
import time
from threading import Event, Thread

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import QueuePool

from hisaab.db import Database, WriterBusy
from hisaab.models import Payee


def _payee(name: str) -> Payee:
    return Payee(normalized_name=name, type="merchant")


def test_write_session_commits_on_success(db: Database) -> None:
    with db.write_session() as s:
        s.add(_payee("Committed"))
    with db.read_session() as s:
        assert s.scalars(select(Payee.normalized_name)).all() == ["Committed"]


def test_write_session_rolls_back_on_exception(db: Database) -> None:
    with pytest.raises(RuntimeError):
        with db.write_session() as s:
            s.add(_payee("Rolled back"))
            s.flush()
            raise RuntimeError("boom")
    with db.read_session() as s:
        assert s.scalars(select(Payee)).all() == []


def test_writers_are_serialized(db: Database) -> None:
    order: list[str] = []
    first_inside: Event = threading.Event()

    def first() -> None:
        with db.write_session():
            order.append("first-start")
            first_inside.set()
            time.sleep(0.15)
            order.append("first-end")

    def second() -> None:
        first_inside.wait()
        with db.write_session():
            order.append("second")

    threads: list[Thread] = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert order == ["first-start", "first-end", "second"]


def test_try_write_session_yields_none_without_waiting_while_lock_is_held(db: Database) -> None:
    with db.writer_lock:
        started: float = time.perf_counter()
        with db.try_write_session() as s:
            assert s is None
        assert time.perf_counter() - started < 0.5  # "didn't wait": waiting would mean seconds; 50 ms was flaky under load


def test_try_write_session_writes_when_lock_is_free(db: Database) -> None:
    with db.try_write_session() as s:
        assert s is not None  # nothing else holds the lock
        s.add(_payee("Best effort"))
    with db.read_session() as s:
        assert s.scalars(select(Payee.normalized_name)).one() == "Best effort"


def test_write_session_times_out_with_writer_busy(db: Database) -> None:
    with db.writer_lock:
        with pytest.raises(WriterBusy):
            with db.write_session(timeout=0.05):
                pass


def test_read_session_cannot_write(db: Database) -> None:
    with db.read_session() as s:
        s.add(_payee("Never saved"))
        with pytest.raises(OperationalError, match="readonly database"):
            s.flush()
    with db.read_session() as s:
        assert s.scalars(select(Payee)).all() == []


def test_read_session_cannot_be_talked_into_writing(db: Database) -> None:
    # query_only is a per-connection switch a handler could flip back; mode=ro is fixed at open.
    with db.read_session() as s:
        s.execute(text("PRAGMA query_only = OFF"))
        with pytest.raises(OperationalError, match="readonly database"):
            s.execute(text("INSERT INTO payees (id, normalized_name, type, created_at) "
                           "VALUES ('x', 'Sneaky', 'merchant', '2026-10-11T00:00:00.000Z')"))


def test_read_during_open_write_transaction_is_not_blocked(db: Database) -> None:
    with db.write_session() as w:
        w.add(_payee("Uncommitted"))
        w.flush()  # the write transaction now holds SQLite's write lock
        started: float = time.perf_counter()
        with db.read_session() as r:
            assert r.scalars(select(Payee)).all() == []  # the last committed snapshot
        assert time.perf_counter() - started < 0.5  # blocked would mean busy_timeout, 5 s
    with db.read_session() as r:
        assert r.scalars(select(Payee.normalized_name)).all() == ["Uncommitted"]


def test_write_engine_has_exactly_one_connection(db: Database) -> None:
    pool: QueuePool = db.write_engine.pool  # type: ignore[assignment]
    assert (pool.size(), pool._max_overflow) == (1, 0)  # no public getter for overflow


def test_interrupt_writer_aborts_only_the_named_threads_statement(db: Database) -> None:
    errors: list[BaseException] = []
    running: Event = threading.Event()

    def long_write() -> None:
        try:
            with db.write_session() as s:
                running.set()
                s.execute(text("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c "
                               "WHERE x < 2000000000) SELECT count(*) FROM c"))
        except BaseException as e:  # noqa: BLE001 - recorded for the assertion
            errors.append(e)

    writer: Thread = threading.Thread(target=long_write)
    writer.start()
    running.wait(5)
    time.sleep(0.1)
    db.interrupt_writer(threading.get_ident())  # this thread is not the writer: nothing happens
    time.sleep(0.2)
    assert writer.is_alive() and errors == []
    assert writer.ident is not None
    db.interrupt_writer(writer.ident)
    writer.join(5)
    assert not writer.is_alive()
    assert len(errors) == 1 and "interrupted" in str(errors[0])
