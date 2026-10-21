"""The discovery sweep (tech-design §5.3, PRD §4.3 track 2): financial language from unregistered senders becomes
candidates holding the sender, subjects and short previews only. Every sender and email here is invented."""

import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, TypeAlias

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import Connection, select, update

from hisaab.clock import LOCAL_TZ
from hisaab.config import Settings
from hisaab.db import Database
from hisaab.ingest import connection
from hisaab.ingest.mailbox import Grant
from hisaab.ingest.sweep import CANDIDATE_CAP, enqueue_due_sweeps, make_sweep
from hisaab.ingest.sync import SyncError, backfill_start
from hisaab.jobs.queue import JobContext, JobQueue
from hisaab.models import GmailConnection, Job, Sender, SenderCandidate, Setting
from tests import factories as f
from tests.gmail_fake import FakeGmail, make_eml

KEY: str = Fernet.generate_key().decode()
NOW_MS: int = int(datetime.now(UTC).timestamp() * 1000)
DAY_MS: int = 86_400_000
BANK: str = "alerts@harbour-bank.example"
BODY: str = ("<p>Dear Customer, Rs.{n}.00 has been debited from your account. "
             + "Please read the terms below carefully. " * 4 + "Secret body line that must stay out.</p>")


@pytest.fixture
def gmail_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"google_oauth_client_id": "client-id", "encryption_key": SecretStr(KEY),
                                       "google_oauth_client_secret": SecretStr("client-secret")})


def connect(db: Database, conn: Connection, address: str = "asha@example.test", name: str = "Asha") -> str:
    uid: str = f.user(conn, display_name=name)["id"]
    conn.commit()
    with db.write_session() as s:
        connection.save_grant(s, uid, Grant(address, FakeGmail().refresh_token), KEY)
    return uid


@pytest.fixture
def uid(db: Database, conn: Connection) -> str:
    f.sender(conn, email_address="alerts@known-bank.example", status="active")
    f.sender(conn, email_address="news@ignored.example", status="ignored")
    conn.commit()
    return connect(db, conn)


Sweep: TypeAlias = Callable[..., dict[str, Any]]


@pytest.fixture
def sweep(db: Database, gmail_settings: Settings) -> Callable[[str, FakeGmail], dict[str, Any]]:
    def run(user_id: str, fake: FakeGmail) -> dict[str, Any]:
        result: dict[str, Any] = make_sweep(gmail_settings, transport=fake, sleep=lambda s: None)(
            JobContext(db, "job", user_id, {}))
        return result
    return run


def gmail(address: str = "asha@example.test", page_size: int = 100) -> FakeGmail:
    return FakeGmail(address=address, page_size=page_size, honor_query=True)


def alert(fake: FakeGmail, mid: str, sender: str = BANK, days_ago: int = 1, n: int = 100, subject: str = "") -> None:
    fake.add(mid, make_eml(sender, subject or f"Rs.{n} debited from a/c", BODY.format(n=n)), NOW_MS - days_ago * DAY_MS)


def candidates(db: Database) -> list[SenderCandidate]:
    with db.read_session() as s:
        rows: list[SenderCandidate] = list(s.scalars(select(SenderCandidate).order_by(SenderCandidate.sender_address)))
        s.expunge_all()
        return rows


def status_of(db: Database, address: str) -> str | None:
    with db.read_session() as s:
        return s.scalar(select(Sender.status).where(Sender.email_address == address))


def test_an_unregistered_financial_sender_becomes_a_candidate_with_previews_only(
        db: Database, uid: str, sweep: Sweep) -> None:
    fake: FakeGmail = gmail()
    for i in range(7):
        alert(fake, f"m{i}", days_ago=i + 1, n=100 + i)
    fake.add("x1", make_eml("friend@mail.example", "Lunch?", "<p>see you</p>"), NOW_MS)  # no financial words
    result: dict[str, Any] = sweep(uid, fake)
    assert (result["new_candidates"], result["examined"]) == (1, 7)
    [row] = candidates(db)
    assert (row.sender_address, row.discovered_by_user_id, row.match_count, row.discarded_at) == (BANK, uid, 7, None)
    examples: list[dict[str, Any]] = json.loads(row.matched_subjects)
    assert len(examples) == 5 and set(examples[0]) == {"subject", "snippet", "received_at"}
    assert examples[0]["subject"] == "Rs.100 debited from a/c"  # newest first
    assert all(len(e["snippet"]) <= 160 for e in examples)
    assert "Secret body line that must stay out" not in row.matched_subjects  # cut off: a preview, not the body
    assert status_of(db, BANK) == "candidate"
    assert set(fake.formats) == {"metadata"}  # no body was ever requested


def test_active_ignored_and_own_mail_never_become_candidates_even_if_gmail_lets_them_through(
        db: Database, uid: str, sweep: Sweep) -> None:
    fake: FakeGmail = gmail()
    fake.honor_query = False  # a query that excludes nothing: the code must filter too
    alert(fake, "a", sender="alerts@known-bank.example")
    alert(fake, "b", sender="news@ignored.example", subject="Weekly credited offers")
    alert(fake, "c", sender="asha@example.test", subject="Fwd: UPI debited")
    assert sweep(uid, fake)["new_candidates"] == 0 and candidates(db) == []
    assert status_of(db, "news@ignored.example") == "ignored"


def test_the_query_excludes_registered_senders_and_starts_at_the_backfill_start(
        db: Database, uid: str, sweep: Sweep) -> None:
    fake: FakeGmail = gmail()
    sweep(uid, fake)
    with db.read_session() as s:
        start: date = backfill_start(s, uid)
    epoch: int = int(datetime.combine(start, time(), LOCAL_TZ).timestamp())
    assert fake.queries[0].endswith(f"-from:alerts@known-bank.example -from:news@ignored.example -from:me after:{epoch}")


def test_the_lookback_is_the_backfill_window_setting(db: Database, conn: Connection, uid: str, sweep: Sweep) -> None:
    conn.execute(update(Setting).where(Setting.key == "backfill_window_months", Setting.user_id == "").values(value="1"))
    conn.commit()
    fake: FakeGmail = gmail()
    alert(fake, "recent", sender="alerts@one.example", days_ago=10)
    alert(fake, "old", sender="alerts@two.example", days_ago=50)  # inside 3 months, outside 1
    sweep(uid, fake)
    assert [c.sender_address for c in candidates(db)] == ["alerts@one.example"]


def test_at_most_twenty_new_candidates_a_sweep_and_the_rest_come_next_time(
        db: Database, uid: str, sweep: Sweep) -> None:
    fake: FakeGmail = gmail(page_size=100)
    for i in range(CANDIDATE_CAP + 1):
        alert(fake, f"m{i:02d}", sender=f"alerts@bank{i:02d}.example", days_ago=1 + i)
    assert sweep(uid, fake)["new_candidates"] == 20 and len(candidates(db)) == 20
    assert sweep(uid, fake)["new_candidates"] == 1 and len(candidates(db)) == 21


def test_a_waiting_candidate_is_refreshed_and_keeps_its_first_seen(db: Database, uid: str, sweep: Sweep) -> None:
    fake: FakeGmail = gmail()
    alert(fake, "m1", days_ago=3)
    sweep(uid, fake)
    first: str = candidates(db)[0].first_seen_at
    alert(fake, "m2", days_ago=0, subject="UPI credited to a/c")
    result: dict[str, Any] = sweep(uid, fake)
    [row] = candidates(db)
    assert (result["new_candidates"], result["refreshed"], row.match_count, row.first_seen_at) == (0, 1, 2, first)
    assert json.loads(row.matched_subjects)[0]["subject"] == "UPI credited to a/c"


def test_one_sender_in_two_inboxes_is_one_sender_and_two_candidates(
        db: Database, conn: Connection, uid: str, sweep: Sweep) -> None:
    other: str = connect(db, conn, "ravi@example.test", "Ravi")
    mine, theirs = gmail(), gmail("ravi@example.test")
    alert(mine, "m1")
    alert(theirs, "t1", days_ago=2)
    sweep(uid, mine)
    sweep(other, theirs)
    assert sorted(c.discovered_by_user_id for c in candidates(db)) == sorted([uid, other])
    with db.read_session() as s:
        assert len(list(s.scalars(select(Sender).where(Sender.email_address == BANK)))) == 1


def test_each_round_excludes_the_senders_already_seen(db: Database, uid: str, sweep: Sweep) -> None:
    fake: FakeGmail = gmail(page_size=3)
    for i in range(50):
        alert(fake, f"busy{i:02d}", sender="alerts@busy.example", days_ago=1)
    alert(fake, "quiet", sender="alerts@quiet.example", days_ago=5)
    result: dict[str, Any] = sweep(uid, fake)
    assert sorted(c.sender_address for c in candidates(db)) == ["alerts@busy.example", "alerts@quiet.example"]
    assert result["examined"] == 4 and "-from:alerts@busy.example" in fake.queries[1]


def test_a_dead_grant_marks_the_connection_and_fails_cleanly(db: Database, uid: str, sweep: Sweep) -> None:
    fake: FakeGmail = gmail()
    fake.revoked = True
    with pytest.raises(SyncError, match="reconnect Gmail"):
        sweep(uid, fake)
    with db.read_session() as s:
        assert s.get_one(GmailConnection, uid).status == "needs_reauth"


# --- scheduling: weekly, surviving restarts -------------------------------------------------------------------

class Recorder(JobQueue):
    def __init__(self, db: Database) -> None:
        super().__init__(db, {})
        self.queued: list[tuple[str, str | None]] = []

    def enqueue(self, kind: str, user_id: str | None = None, params: dict[str, Any] | None = None) -> str:
        self.queued.append((kind, user_id))
        return "job"


def test_a_sweep_is_queued_for_inboxes_never_swept_or_swept_a_week_ago(
        db: Database, conn: Connection, uid: str) -> None:
    recent: str = connect(db, conn, "ravi@example.test", "Ravi")
    stale: str = connect(db, conn, "meera@example.test", "Meera")
    dead: str = connect(db, conn, "old@example.test", "Old")
    now: datetime = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    for who, days in ((recent, 2), (stale, 8)):
        f.job(conn, user_id=who, kind="sweep", state="succeeded",
              created_at=(now - timedelta(days=days)).isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    conn.execute(update(GmailConnection).where(GmailConnection.user_id == dead).values(status="needs_reauth"))
    conn.commit()
    queue: Recorder = Recorder(db)
    enqueue_due_sweeps(queue, now)
    assert sorted(queue.queued) == sorted([("sweep", uid), ("sweep", stale)])


def test_a_long_ignore_list_keeps_the_sweep_working_and_ignored_mail_out(
        db: Database, conn: Connection, uid: str, sweep: Sweep) -> None:
    from hisaab.ingest.mailbox import MAX_QUERY_CHARS
    for i in range(300):
        f.sender(conn, email_address=f"news{i:03d}@long-newsletter-domain.example", status="ignored",
                 reviewed_at=f"2026-10-{1 + i % 28:02d}T00:00:00.000Z")
    conn.commit()
    fake: FakeGmail = gmail()
    alert(fake, "n", sender="news150@long-newsletter-domain.example", subject="Cashback credited")
    alert(fake, "b", sender=BANK)
    assert sweep(uid, fake)["new_candidates"] == 1
    assert [c.sender_address for c in candidates(db)] == [BANK]
    assert all(len(q) <= MAX_QUERY_CHARS for q in fake.queries)
    assert "-from:alerts@known-bank.example" in fake.queries[0]  # active senders are kept before ignored ones


def test_a_sender_without_an_address_is_skipped_and_never_put_in_the_query(
        db: Database, uid: str, sweep: Sweep) -> None:
    fake: FakeGmail = gmail(page_size=1)
    alert(fake, "n1", sender="Bank Alerts", days_ago=1)  # a display name only: parseaddr gives "bank" or ""
    alert(fake, "n2", sender="undisclosed-recipients:;", days_ago=2)
    alert(fake, "b", sender=BANK, days_ago=3)
    sweep(uid, fake)
    assert [c.sender_address for c in candidates(db)] == [BANK]
    assert not any(re.search(r"-from:(\s|$)", q) or "-from:bank " in q for q in fake.queries)


def test_another_household_members_address_never_becomes_a_candidate(
        db: Database, conn: Connection, uid: str, sweep: Sweep) -> None:
    connect(db, conn, "ravi@example.test", "Ravi")
    fake: FakeGmail = gmail()
    alert(fake, "r", sender="ravi@example.test", subject="UPI credited? did it reach you")
    alert(fake, "b", sender=BANK)
    sweep(uid, fake)
    assert [c.sender_address for c in candidates(db)] == [BANK]


def test_one_unreadable_email_is_counted_and_the_sweep_goes_on(db: Database, uid: str, sweep: Sweep) -> None:
    fake: FakeGmail = gmail()
    alert(fake, "gone", sender="alerts@gone.example", days_ago=1)
    alert(fake, "b", sender=BANK, days_ago=2)
    fake.broken_ids.add("gone")  # deleted between the search and the read
    result: dict[str, Any] = sweep(uid, fake)
    assert (result["unreadable"], [c.sender_address for c in candidates(db)]) == (1, [BANK])
