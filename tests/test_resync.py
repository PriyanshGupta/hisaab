"""Re-sync a date range (tech-design §5.2): stored messages in the range are fetched again and re-parsed, and
their transactions updated PER FIELD — edited fields untouched, statement values untouched, everything else from
the new parse. The clock never moves. Every email here is invented."""

import copy
import json
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import Connection, select, update

from hisaab.accounts import repository as accounts
from hisaab.clock import LOCAL_TZ
from hisaab.config import Settings
from hisaab.db import Database
from hisaab.ingest import connection
from hisaab.ingest.mailbox import Grant
from hisaab.ingest.resync import affected, enqueue_resync, resolve_notices
from hisaab.ingest.sync import make_sync
from hisaab.jobs.queue import JobContext, JobQueue
from hisaab.models import (
    GmailConnection,
    ParserTemplate,
    ResyncNotice,
    Source,
    Transaction,
    TransactionEffective,
    TransactionSource,
)
from hisaab.parsing import templates
from hisaab.parsing.statement_interpreter import StatementRow
from hisaab.pipeline.derive import apply_statement
from tests import factories as f
from tests.gmail_fake import FakeGmail, make_eml
from tests.pipeline_helpers import add_source
from tests.test_email_interpreter import BODY
from tests.test_email_spec import SPEC

KEY: str = Fernet.generate_key().decode()
BANK: str = "alerts@example-bank.in"
IN_A: int = int(datetime(2026, 10, 6, 4, 0, tzinfo=UTC).timestamp() * 1000)
IN_B: int = int(datetime(2026, 10, 6, 5, 0, tzinfo=UTC).timestamp() * 1000)
OUT: int = int(datetime(2026, 9, 20, 4, 0, tzinfo=UTC).timestamp() * 1000)
RANGE: dict[str, str] = {"from": "2026-10-05", "to": "2026-10-07"}


@pytest.fixture
def gmail_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"google_oauth_client_id": "client-id", "encryption_key": SecretStr(KEY),
                                       "google_oauth_client_secret": SecretStr("client-secret")})


def connect(db: Database, conn: Connection, address: str = "asha@example.test") -> str:
    uid: str = f.user(conn)["id"]
    conn.commit()
    with db.write_session() as s:
        connection.save_grant(s, uid, Grant(address, FakeGmail().refresh_token), KEY)
    return uid


@pytest.fixture
def uid(db: Database, conn: Connection) -> str:
    f.sender(conn, email_address=BANK, status="active")
    conn.commit()
    user_id: str = connect(db, conn)
    with db.write_session() as s:
        accounts.create_account(s, user_id, type="bank", display_name="Savings", opening_balance=0,
                                opening_balance_date="2026-09-01", last4="1111")
        no_date: dict[str, Any] = copy.deepcopy(SPEC)
        no_date["fields"]["date"] = {"required": False}  # "broken": reads the received date, not the body's
        templates.save_email_template(s, name="Example debit", spec=no_date, status="active")
    return user_id


def fake_gmail() -> FakeGmail:
    fake: FakeGmail = FakeGmail(page_size=2, honor_query=True)
    for mid, when, ref in (("a", IN_A, "9988776655"), ("b", IN_B, "1122334455"), ("c", OUT, "5544332211")):
        header: str = format_datetime(datetime.fromtimestamp(when / 1000, UTC).astimezone(LOCAL_TZ))
        fake.add(mid, make_eml(BANK, "Debit alert", BODY.replace("9988776655", ref), date=header), when)
    return fake


def run(db: Database, settings: Settings, fake: FakeGmail, uid: str, params: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = make_sync(settings, transport=fake, sleep=lambda s: None)(JobContext(db, "job", uid, params))
    return result


def txn_of(db: Database, message_id: str) -> TransactionEffective:
    with db.read_session() as s:
        t: TransactionEffective = s.scalars(select(TransactionEffective)
                                            .join(TransactionSource, TransactionSource.transaction_id == TransactionEffective.id)
                                            .join(Source, Source.id == TransactionSource.source_id)
                                            .where(Source.external_message_id == message_id)).one()
        s.expunge(t)
        return t


def test_a_resync_rereads_the_range_per_field_and_leaves_the_clock(
        db: Database, conn: Connection, uid: str, gmail_settings: Settings) -> None:
    fake: FakeGmail = fake_gmail()
    run(db, gmail_settings, fake, uid, {})  # the first backfill stores all three
    a, b = txn_of(db, "a"), txn_of(db, "b")
    assert a.transaction_date == b.transaction_date == "2026-10-06"  # the received date in India: the template's flaw
    # the user corrects a's payee; a real statement upload matches b (by its reference) at another amount
    payee: str = f.payee(conn, normalized_name="My Corner Shop")["id"]
    f.transaction_edit(conn, a.id, uid, field="payee_id", new_value=payee)
    conn.commit()
    with db.write_session() as s:
        account: str = s.scalars(select(Transaction.from_account_id).where(Transaction.id == b.id)).one() or ""
        statement: Source = add_source(s, uid, b"%PDF statement", None, kind="statement", processing_state="parsed")
        apply_statement(s, uid, statement, account, [StatementRow(
            page=1, date="2026-10-07", time=None, narration="CORNER SHOP", reference="1122334455", direction="debit",
            amount=123400, balance=None)])
    assert (txn_of(db, "b").amount, txn_of(db, "b").posted_date) == (123400, "2026-10-07")
    conn.execute(update(ParserTemplate).values(spec=json.dumps(SPEC)))  # the template, fixed
    clock: str | None = conn.execute(select(GmailConnection.last_successful_sync_at)).scalar()
    conn.commit()
    fake.calls.clear()
    result: dict[str, Any] = run(db, gmail_settings, fake, uid, RANGE)
    assert result["reparsed"] == 2 and result["stored"] == 0
    assert not any(c.endswith("/messages/c") for c in fake.calls)  # outside the range: not fetched
    a2, b2 = txn_of(db, "a"), txn_of(db, "b")
    assert (a2.id, a2.transaction_date, a2.payee_id) == (a.id, "2026-10-05", payee)  # date fixed, edit kept
    # the statement's amount and posted date stand; the date the email reads takes the new parse (amendment 11)
    assert (b2.id, b2.amount, b2.posted_date, b2.transaction_date) == (b.id, 123400, "2026-10-07", "2026-10-05")
    with db.read_session() as s:
        assert s.scalar(select(GmailConnection.last_successful_sync_at)) == clock
    assert any(q.startswith(f"(from:{BANK}) after:") and " before:" in q for q in fake.queries[-1:])


def test_affected_counts_transactions_with_an_email_in_the_range_per_inbox(
        db: Database, conn: Connection, uid: str, gmail_settings: Settings) -> None:
    run(db, gmail_settings, fake_gmail(), uid, {})
    other: str = connect(db, conn, "ravi@example.test")
    f.transaction_source(conn, f.transaction(conn, other)["id"],
                         f.source(conn, other, kind="email", received_at="2026-10-06T06:00:00.000Z")["id"])
    conn.commit()
    with db.read_session() as s:
        assert affected(s, "2026-10-05", "2026-10-07", None) == 3
        assert affected(s, "2026-10-05", "2026-10-07", uid) == 2
        assert affected(s, "2026-10-05", "2026-10-07", other) == 1
        assert affected(s, "2026-09-01", "2026-09-30", uid) == 1


class Recorder(JobQueue):
    def __init__(self, db: Database) -> None:
        super().__init__(db, {})
        self.queued: list[tuple[str, str | None, dict[str, Any] | None]] = []

    def enqueue(self, kind: str, user_id: str | None = None, params: dict[str, Any] | None = None) -> str:
        self.queued.append((kind, user_id, params))
        return f"job-{len(self.queued)}"


def test_a_resync_is_queued_for_every_connected_inbox_or_one(db: Database, conn: Connection, uid: str) -> None:
    other: str = connect(db, conn, "ravi@example.test")
    dead: str = connect(db, conn, "old@example.test")
    conn.execute(update(GmailConnection).where(GmailConnection.user_id == dead).values(status="needs_reauth"))
    conn.commit()
    queue: Recorder = Recorder(db)
    enqueue_resync(queue, "2026-10-05", "2026-10-07", None)
    assert sorted(u or "" for _, u, _ in queue.queued) == sorted([uid, other])
    assert {k for k, _, _ in queue.queued} == {"resync"} and queue.queued[0][2] == RANGE
    queue.queued.clear()
    enqueue_resync(queue, "2026-10-05", "2026-10-07", other)
    assert [u for _, u, _ in queue.queued] == [other]


def test_starting_a_resync_resolves_the_notices_it_covers(db: Database, conn: Connection) -> None:
    inside: str = f.resync_notice(conn, range_from="2026-10-05", range_to="2026-10-06")["id"]
    wider: str = f.resync_notice(conn, range_from="2026-09-01", range_to="2026-10-06")["id"]
    conn.commit()
    with db.write_session() as s:
        assert resolve_notices(s, "2026-10-01", "2026-10-07") == 1
    with db.read_session() as s:
        assert s.get_one(ResyncNotice, inside).resolved_at is not None
        assert s.get_one(ResyncNotice, wider).resolved_at is None


def test_two_resyncs_for_one_inbox_are_two_jobs(db: Database, uid: str, settings: Settings) -> None:
    """Slice 24 review: joining would swallow the second range (jobs of one kind join per user, whatever their
    params) while its notices were marked done. The queue runs one job at a time anyway."""
    from hisaab.jobs.kinds import job_kinds
    queue: JobQueue = JobQueue(db, job_kinds(settings))  # not started: the jobs stay queued
    first: list[str] = enqueue_resync(queue, "2026-07-01", "2026-07-31", uid)
    second: list[str] = enqueue_resync(queue, "2026-09-01", "2026-09-30", uid)
    assert first != second and len(first) == len(second) == 1
    assert (job_kinds(settings)["resync"].timeout, job_kinds(settings)["resync"].joins) == (30 * 60, False)


def test_a_reparse_that_flips_debit_to_credit_moves_the_account_to_the_other_side(
        db: Database, conn: Connection, uid: str) -> None:
    """Slice 24 review: re-sync exists for broken templates, so a fix that reads the other direction must move the
    account to the other leg, not leave it on both (a transfer from an account to itself)."""
    from hisaab.pipeline.derive import apply
    from hisaab.pipeline.routing import Routed, route
    from tests.test_routing import Llm
    with db.write_session() as s:
        src: str = add_source(s, uid, make_eml(BANK, "Debit alert", BODY), "flip-1").id
    with db.read_session() as s:
        first: Routed = route(s, s.get_one(Source, src), Llm(), "m")
    with db.write_session() as s:
        apply(s, uid, [(src, first)])
    credit: dict[str, Any] = copy.deepcopy(SPEC)
    credit["fields"]["direction"] = {"const": "credit"}  # the fix: this alert was money in all along
    conn.execute(update(ParserTemplate).values(spec=json.dumps(credit)))
    conn.execute(update(Source).where(Source.id == src).values(  # as re-sync's _requeue does
        processing_state="pending", content=make_eml(BANK, "Debit alert", BODY)))
    conn.commit()
    with db.read_session() as s:
        again: Routed = route(s, s.get_one(Source, src), Llm(), "m")
    with db.write_session() as s:
        apply(s, uid, [(src, again)])
    t: TransactionEffective = txn_of(db, "flip-1")
    assert (t.kind, t.from_account_id is None, t.to_account_id is not None) == ("income", True, True)
