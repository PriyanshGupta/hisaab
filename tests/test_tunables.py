"""PRD §10.1 tunables from the settings table: a user's own row wins over the app row."""

from sqlalchemy import Connection

from hisaab.db import Database
from hisaab.tunables import tunable
from tests import factories as f


def test_app_level_seed_value(db: Database) -> None:
    with db.read_session() as s:
        assert tunable(s, "sync_min_lookback_hours") == 48  # seeded by migration 0001


def test_user_row_overrides_app_row(db: Database, conn: Connection) -> None:
    uid: str = f.user(conn)["id"]
    f.setting(conn, key="sync_min_lookback_hours", user_id=uid, value="72")
    conn.commit()
    with db.read_session() as s:
        assert tunable(s, "sync_min_lookback_hours", uid) == 72
        assert tunable(s, "sync_min_lookback_hours", "someone-else") == 48
