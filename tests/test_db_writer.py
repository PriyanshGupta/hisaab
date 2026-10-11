"""The single writer lock (write-path ruling, 2026-10-10)."""

import threading
import time

import pytest
from sqlalchemy import select

from hisaab.db import Database, WriterBusy
from hisaab.models import Payee


@pytest.fixture
def db(migrated_engine):
    return Database(migrated_engine)


def _payee(name):
    return Payee(normalized_name=name, type="merchant")


def test_write_session_commits_on_success(db):
    with db.write_session() as s:
        s.add(_payee("Committed"))
    with db.read_session() as s:
        assert s.scalars(select(Payee.normalized_name)).all() == ["Committed"]


def test_write_session_rolls_back_on_exception(db):
    with pytest.raises(RuntimeError):
        with db.write_session() as s:
            s.add(_payee("Rolled back"))
            s.flush()
            raise RuntimeError("boom")
    with db.read_session() as s:
        assert s.scalars(select(Payee)).all() == []


def test_writers_are_serialized(db):
    order = []
    first_inside = threading.Event()

    def first():
        with db.write_session():
            order.append("first-start")
            first_inside.set()
            time.sleep(0.15)
            order.append("first-end")

    def second():
        first_inside.wait()
        with db.write_session():
            order.append("second")

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert order == ["first-start", "first-end", "second"]


def test_try_write_session_yields_none_without_waiting_while_lock_is_held(db):
    with db.writer_lock:
        started = time.perf_counter()
        with db.try_write_session() as s:
            assert s is None
        assert time.perf_counter() - started < 0.05


def test_try_write_session_writes_when_lock_is_free(db):
    with db.try_write_session() as s:
        s.add(_payee("Best effort"))
    with db.read_session() as s:
        assert s.scalars(select(Payee.normalized_name)).one() == "Best effort"


def test_write_session_times_out_with_writer_busy(db):
    with db.writer_lock:
        with pytest.raises(WriterBusy):
            with db.write_session(timeout=0.05):
                pass


def test_read_session_never_commits(db, conn):
    with db.read_session() as s:
        s.add(_payee("Never saved"))
        s.flush()
    with db.read_session() as s:
        assert s.scalars(select(Payee)).all() == []
