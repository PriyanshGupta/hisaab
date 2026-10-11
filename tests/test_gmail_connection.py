"""gmail_connections: the encrypted grant, its status, and the sync clock (tech-design §3.3, §5.5)."""

from cryptography.fernet import Fernet
from sqlalchemy import Connection

from hisaab.db import Database
from hisaab.ingest import connection
from hisaab.ingest.mailbox import Grant
from hisaab.models import GmailConnection
from tests import factories as f

KEY: str = Fernet.generate_key().decode()


def stored(db: Database, user_id: str) -> GmailConnection:
    with db.read_session() as s:
        row: GmailConnection = s.get_one(GmailConnection, user_id)
        s.expunge(row)
        return row


def test_a_grant_is_stored_encrypted_and_connected(db: Database, conn: Connection) -> None:
    uid: str = f.user(conn)["id"]
    conn.commit()
    with db.write_session() as s:
        connection.save_grant(s, uid, Grant("asha@example.test", "1//secret-refresh"), KEY)
    row: GmailConnection = stored(db, uid)
    assert (row.email_address, row.status, row.last_successful_sync_at) == ("asha@example.test", "connected", None)
    assert b"secret-refresh" not in row.refresh_token_enc
    assert connection.credential(row, KEY) == "1//secret-refresh"


def test_reconnecting_the_same_inbox_keeps_the_clock(db: Database, conn: Connection) -> None:
    uid: str = f.gmail_connection(conn, email_address="asha@example.test", status="needs_reauth",
                                  last_successful_sync_at="2026-10-09T00:00:00.000Z")["user_id"]
    conn.commit()
    with db.write_session() as s:
        connection.save_grant(s, uid, Grant("asha@example.test", "1//new"), KEY)
    row: GmailConnection = stored(db, uid)
    assert (row.status, row.last_successful_sync_at) == ("connected", "2026-10-09T00:00:00.000Z")  # catch-up covers the gap
    assert connection.credential(row, KEY) == "1//new"


def test_connecting_a_different_inbox_resets_the_clock(db: Database, conn: Connection) -> None:
    uid: str = f.gmail_connection(conn, email_address="asha@example.test",
                                  last_successful_sync_at="2026-10-09T00:00:00.000Z")["user_id"]
    conn.commit()
    with db.write_session() as s:
        connection.save_grant(s, uid, Grant("asha.other@example.test", "1//other"), KEY)
    assert stored(db, uid).last_successful_sync_at is None


def test_needs_reauth_leaves_the_clock(db: Database, conn: Connection) -> None:
    uid: str = f.gmail_connection(conn, last_successful_sync_at="2026-10-09T00:00:00.000Z")["user_id"]
    conn.commit()
    with db.write_session() as s:
        connection.mark_needs_reauth(s, uid)
    row: GmailConnection = stored(db, uid)
    assert (row.status, row.last_successful_sync_at) == ("needs_reauth", "2026-10-09T00:00:00.000Z")
