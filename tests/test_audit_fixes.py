"""Regression tests for the code audit of 2026-10-11 (findings 1-4)."""

import threading
import time
from datetime import UTC, date, datetime

import pytest
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select

from hisaab.accounts import repository as accounts
from hisaab.auth import passwords
from hisaab.auth.throttle import LoginThrottle
from hisaab.clock import local_today
from hisaab.models import UserSession
from tests import factories as f
from tests.conftest import login, make_user


class StandInHasher:
    """Replaces the Argon2 hasher (whose methods can't be patched: __slots__)."""

    def __init__(self, verify):
        self.verify = verify

    def check_needs_rehash(self, _hash):
        return False

    def hash(self, _password):
        return "stand-in-hash"


# --- 1a. Argon2 runs at most 2 at a time (64 MiB each) ------------------------------

def test_password_checks_never_run_more_than_two_at_once(monkeypatch):
    running, peak, lock = 0, 0, threading.Lock()

    def slow_verify(_hash, _password):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        return True

    monkeypatch.setattr(passwords, "_hasher", StandInHasher(verify=slow_verify))
    threads = [threading.Thread(target=passwords.verify_password, args=("h", "pw")) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 2


# --- 1b. Failed sign-ins lock a username for a while --------------------------------

class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def test_throttle_locks_after_ten_failures_and_unlocks_later():
    clock = FakeClock()
    t = LoginThrottle(clock=clock)
    for _ in range(9):
        t.record_failure("asha")
    assert not t.is_locked("asha")
    t.record_failure("asha")
    assert t.is_locked("asha")
    clock.now += 15 * 60 + 1
    assert not t.is_locked("asha")


def test_throttle_success_resets_the_count():
    t = LoginThrottle(clock=FakeClock())
    for _ in range(9):
        t.record_failure("asha")
    t.record_success("asha")
    t.record_failure("asha")
    assert not t.is_locked("asha")


def test_throttle_memory_stays_bounded_under_a_username_spray():
    clock = FakeClock()
    t = LoginThrottle(clock=clock, max_tracked=100)
    for i in range(1000):
        t.record_failure(f"user{i}")
        clock.now += 15 * 60 + 1  # each entry has expired by the time the next arrives
    assert len(t._entries) <= 100


@pytest.fixture
def asha(app):
    return make_user(app, "asha", "correct horse battery")


def test_eleventh_attempt_is_refused_even_with_the_right_password(client, asha):
    for _ in range(10):
        assert login(client, "asha", "wrong password!").status_code == 401
    resp = login(client, "ASHA", "correct horse battery")  # same username, any case
    assert resp.status_code == 429
    assert "15 minutes" in resp.json()["detail"]


def test_unknown_usernames_lock_too_so_lockout_reveals_nothing(client):
    for _ in range(10):
        login(client, "nobody", "wrong password!")
    assert login(client, "nobody", "wrong password!").status_code == 429


def test_a_locked_username_costs_no_password_hashing(client, asha, monkeypatch):
    for _ in range(10):
        login(client, "asha", "wrong password!")
    calls = []

    def recording_verify(*args):  # every path (real user or dummy) ends in _hasher.verify
        calls.append(args)
        raise VerifyMismatchError

    monkeypatch.setattr(passwords, "_hasher", StandInHasher(verify=recording_verify))
    assert login(client, "asha", "wrong password!").status_code == 429
    assert calls == []


def test_other_usernames_are_unaffected(app, client, asha):
    make_user(app, "ravi", "ravi password 1")
    for _ in range(10):
        login(client, "asha", "wrong password!")
    assert login(client, "ravi", "ravi password 1").status_code == 200


# --- 2. "Today" is India's date, not the UTC server's ------------------------------

def test_local_today_is_india_time():
    # 20:00 UTC on 11 Oct is 01:30 on 12 Oct in India.
    assert local_today(datetime(2026, 10, 11, 20, 0, tzinfo=UTC)) == date(2026, 10, 12)
    assert local_today(datetime(2026, 10, 11, 18, 0, tzinfo=UTC)) == date(2026, 10, 11)


def test_opening_date_limit_uses_india_today(conn, monkeypatch, migrated_engine):
    from hisaab.db import Database

    uid = f.user(conn)["id"]
    conn.commit()
    db = Database(migrated_engine)
    monkeypatch.setattr(accounts, "local_today", lambda: date(2030, 1, 2))
    with db.write_session() as s:
        accounts.create_account(s, uid, type="cash", display_name="Wallet", opening_balance=0,
                                opening_balance_date="2030-01-02")  # "today" in India: accepted
    with pytest.raises(accounts.AccountError, match="future"):
        with db.write_session() as s:
            accounts.create_account(s, uid, type="cash", display_name="Wallet 2", opening_balance=0,
                                    opening_balance_date="2030-01-03")


# --- 3. Amounts are bounded ---------------------------------------------------------

@pytest.mark.parametrize("paise", [10**15 + 1, -(10**15 + 1), 10**20])
def test_absurd_amounts_are_refused_not_crashed_on(client, asha, paise):
    login(client, "asha", "correct horse battery")
    resp = client.post("/api/accounts", json={
        "type": "bank", "display_name": "Big", "last4": "1234",
        "opening_balance_paise": paise, "opening_balance_date": "2026-07-11",
    })
    assert resp.status_code == 422
    assert "too large" in resp.json()["detail"]


def test_the_largest_allowed_amount_is_accepted(client, asha):
    login(client, "asha", "correct horse battery")
    resp = client.post("/api/accounts", json={
        "type": "bank", "display_name": "Big", "last4": "1234",
        "opening_balance_paise": 10**15, "opening_balance_date": "2026-07-11",
    })
    assert resp.status_code == 201


# --- 4. Signing out works even while a long job holds the writer --------------------

def test_sign_out_clears_the_cookie_even_when_the_writer_is_busy(app, client, asha):
    login(client, "asha", "correct horse battery")
    started = time.perf_counter()
    with app.state.db.writer_lock:
        resp = client.post("/api/auth/logout")
    assert resp.status_code == 204
    assert time.perf_counter() - started < 1  # did not wait for the writer
    assert "max-age=0" in resp.headers["set-cookie"].lower()
    assert client.get("/api/auth/me").status_code == 401


def test_sign_out_deletes_the_session_row_when_it_can(app, client, asha):
    login(client, "asha", "correct horse battery")
    client.post("/api/auth/logout")
    with app.state.db.read_session() as s:
        assert s.scalars(select(UserSession)).all() == []
