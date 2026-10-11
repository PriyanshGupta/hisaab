"""The sync job (tech-design §5.1, §5.6) against a fake Gmail."""

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeAlias

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import Connection, func, select, update

from hisaab.config import Settings
from hisaab.db import Database
from hisaab.ingest import connection
from hisaab.ingest.mailbox import Grant
from hisaab.ingest.sync import SyncError, make_sync
from hisaab.jobs.queue import JobContext
from hisaab.models import GmailConnection, Sender, Source, utc_now
from tests import factories as f
from tests.gmail_fake import FakeGmail, make_eml

KEY: str = Fernet.generate_key().decode()
BANK: str = "alerts@bank.example"
NOW_MS: int = int(datetime.now(UTC).timestamp() * 1000)


@pytest.fixture
def gmail_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"google_oauth_client_id": "client-id", "encryption_key": SecretStr(KEY),
                                       "google_oauth_client_secret": SecretStr("client-secret")})


@pytest.fixture
def fake() -> FakeGmail:
    gmail: FakeGmail = FakeGmail(page_size=2)
    for i in range(3):
        gmail.add(f"m{i}", make_eml(BANK, f"Alert {i}", f"<p>Rs. {i}00.00 debited</p>"), NOW_MS - i * 60_000)
    gmail.add("m-statement", make_eml(BANK, "Statement", "<p>attached</p>", attachment=("statement.pdf", b"%PDF-1.7")),
              NOW_MS - 3_600_000)
    return gmail


@pytest.fixture
def user_id(db: Database, conn: Connection) -> str:
    uid: str = f.user(conn)["id"]
    f.sender(conn, email_address=BANK, status="active")
    conn.commit()
    with db.write_session() as s:
        connection.save_grant(s, uid, Grant("asha@example.test", FakeGmail().refresh_token), KEY)
    return uid


Run: TypeAlias = Callable[..., dict[str, Any]]


@pytest.fixture
def sync(db: Database, gmail_settings: Settings, fake: FakeGmail) -> Run:
    def run(user_id: str) -> dict[str, Any]:
        result: dict[str, Any] = make_sync(gmail_settings, http=fake, sleep=lambda s: None)(JobContext(db, "job", user_id, {}))
        return result
    return run


def clock(db: Database, user_id: str) -> str | None:
    with db.read_session() as s:
        return s.get_one(GmailConnection, user_id).last_successful_sync_at


def status(db: Database, user_id: str) -> str:
    with db.read_session() as s:
        return s.get_one(GmailConnection, user_id).status


def source_count(db: Database) -> int:
    with db.read_session() as s:
        return s.scalar(select(func.count()).select_from(Source)) or 0


def test_first_sync_stores_each_message_as_a_pending_source(db: Database, user_id: str, sync: Run) -> None:
    before: str = utc_now()
    result: dict[str, Any] = sync(user_id)
    assert {k: result[k] for k in ("stored", "already_had", "failed", "with_attachments")} == \
        {"stored": 4, "already_had": 0, "failed": 0, "with_attachments": 1}
    with db.read_session() as s:
        rows: list[Source] = list(s.scalars(select(Source).order_by(Source.ingest_seq)))
        assert {r.external_message_id for r in rows} == {"m0", "m1", "m2", "m-statement"}
        for r in rows:
            assert (r.kind, r.processing_state, r.user_id) == ("email", "pending", user_id)
            assert r.content is not None and r.content_hash == hashlib.sha256(r.content).hexdigest()
        statement: Source = next(r for r in rows if r.external_message_id == "m-statement")
        assert json.loads(statement.attachments or "null") == [
            {"filename": "statement.pdf", "size": 8, "mime_type": "application/pdf"}]
        assert b"%PDF" not in (statement.content or b"")
        assert [r.attachments for r in rows if r.external_message_id != "m-statement"] == [None, None, None]
    stamped: str | None = clock(db, user_id)
    assert stamped is not None and before <= stamped <= utc_now()


def test_syncing_the_same_window_again_stores_zero_duplicates(db: Database, user_id: str, sync: Run,
                                                               fake: FakeGmail) -> None:
    sync(user_id)
    fake.calls.clear()
    again: dict[str, Any] = sync(user_id)
    assert (again["stored"], again["already_had"]) == (0, 4)
    assert source_count(db) == 4
    assert not [c for c in fake.calls if "/messages/" in c]  # known ids are not fetched again


def test_the_clock_is_the_time_the_sync_started(db: Database, user_id: str, gmail_settings: Settings,
                                               fake: FakeGmail) -> None:
    first_call: list[str] = []

    def http(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, bytes]:
        if not first_call:
            first_call.append(utc_now())
            time.sleep(0.01)
        return fake(method, url, body, headers)

    make_sync(gmail_settings, http=http, sleep=lambda s: None)(JobContext(db, "job", user_id, {}))
    stamped: str | None = clock(db, user_id)
    assert stamped is not None and stamped <= first_call[0]  # mail arriving mid-sync is inside the next window


def test_the_query_names_active_senders_and_the_48_hour_minimum(user_id: str, sync: Run, fake: FakeGmail,
                                                                conn: Connection) -> None:
    f.sender(conn, email_address="news@bank.example", status="ignored")
    conn.commit()
    started: datetime = datetime.now(UTC)
    sync(user_id)
    query: str = fake.queries[0]
    assert query.startswith(f"(from:{BANK}) after:")
    assert abs(int(query.rsplit(":", 1)[1]) - int((started - timedelta(hours=48)).timestamp())) <= 2


def test_after_a_long_gap_the_window_reaches_back_to_the_last_sync_less_a_day(
    db: Database, user_id: str, sync: Run, fake: FakeGmail,
) -> None:
    last: datetime = datetime.now(UTC) - timedelta(days=5)
    with db.write_session() as s:
        s.get_one(GmailConnection, user_id).last_successful_sync_at = utc_now(last)
    sync(user_id)
    assert abs(int(fake.queries[0].rsplit(":", 1)[1]) - int((last - timedelta(days=1)).timestamp())) <= 1


def test_one_message_failing_keeps_the_rest_and_holds_the_clock(db: Database, user_id: str, sync: Run,
                                                                fake: FakeGmail) -> None:
    fake.broken_ids = {"m1"}
    result: dict[str, Any] = sync(user_id)
    assert (result["stored"], result["failed"]) == (3, 1)
    assert clock(db, user_id) is None  # not a full success: the next run must cover m1 again
    fake.broken_ids = set()
    assert sync(user_id)["stored"] == 1
    assert clock(db, user_id) is not None and source_count(db) == 4


def test_a_revoked_grant_moves_to_needs_reauth_and_holds_the_clock(db: Database, user_id: str, sync: Run,
                                                                   fake: FakeGmail) -> None:
    with db.write_session() as s:
        s.get_one(GmailConnection, user_id).last_successful_sync_at = "2026-10-09T00:00:00.000Z"
    fake.revoked = True
    with pytest.raises(SyncError, match="reconnect"):
        sync(user_id)
    assert (status(db, user_id), clock(db, user_id)) == ("needs_reauth", "2026-10-09T00:00:00.000Z")
    assert source_count(db) == 0


def test_a_grant_the_current_key_cannot_decrypt_needs_reauth(db: Database, user_id: str, sync: Run) -> None:
    with db.write_session() as s:
        connection.save_grant(s, user_id, Grant("asha@example.test", "x"), Fernet.generate_key().decode())
    with pytest.raises(SyncError, match="reconnect"):
        sync(user_id)
    assert status(db, user_id) == "needs_reauth"


def test_a_user_who_needs_to_reconnect_is_not_synced(db: Database, user_id: str, sync: Run, fake: FakeGmail) -> None:
    with db.write_session() as s:
        connection.mark_needs_reauth(s, user_id)
    with pytest.raises(SyncError, match="reconnect"):
        sync(user_id)
    assert fake.calls == []


def test_with_no_active_senders_nothing_is_searched(db: Database, user_id: str, sync: Run, fake: FakeGmail,
                                                    conn: Connection) -> None:
    conn.execute(update(Sender).values(status="ignored"))
    conn.commit()
    assert sync(user_id)["stored"] == 0
    assert fake.queries == []
    assert clock(db, user_id) is not None
