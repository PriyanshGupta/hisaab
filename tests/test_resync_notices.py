"""Parser-change notices (tech-design §5.2): editing, approving or updating a shipped template writes a pending
re-sync notice naming the template, why, and the date range of the emails it read. Invented data only."""

import copy
import json
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, select

from hisaab.db import Database
from hisaab.models import ParserTemplate, ResyncNotice
from hisaab.parsing.notices import write_notice
from hisaab.seeds import sync_seeds
from tests import factories as f
from tests.test_email_spec import SPEC
from tests.test_seeds import PATTERNS


def read_by(conn: Connection, user_id: str, template: str, received: str, route: str = "tier1") -> None:
    f.source(conn, user_id, received_at=received, diagnostic=json.dumps({"route": route, "template": template}))


def notices(db: Database) -> list[tuple[str, str, str, str]]:
    with db.read_session() as s:
        return [(n.template_name, n.reason, n.range_from, n.range_to)
                for n in s.scalars(select(ResyncNotice).order_by(ResyncNotice.created_at))]


def test_the_range_is_the_india_dates_of_the_emails_that_template_read(db: Database, conn: Connection) -> None:
    uid: str = f.user(conn)["id"]
    tid: str = f.parser_template(conn, name="lantern_debit", origin="llm_proposed", spec=json.dumps(SPEC))["id"]
    read_by(conn, uid, "lantern_debit", "2026-07-12T20:00:00.000Z")  # 13 Jul, 01:30 in India
    read_by(conn, uid, "lantern_debit", "2026-10-18T04:00:00.000Z")
    read_by(conn, uid, "other_template", "2026-06-01T04:00:00.000Z")
    read_by(conn, uid, "lantern_debit", "2026-05-01T04:00:00.000Z", route="cache")  # the model read it, not this
    conn.commit()
    with db.write_session() as s:
        write_notice(s, s.get_one(ParserTemplate, tid), "edited")
    assert notices(db) == [("lantern_debit", "edited", "2026-07-13", "2026-10-18")]


def test_a_template_that_read_nothing_gets_no_notice(db: Database, conn: Connection) -> None:
    tid: str = f.parser_template(conn, name="unused", spec=json.dumps(SPEC))["id"]
    conn.commit()
    with db.write_session() as s:
        assert write_notice(s, s.get_one(ParserTemplate, tid), "edited") is None
    assert notices(db) == []


def test_explicit_dates_cover_emails_no_longer_marked_with_the_template(db: Database, conn: Connection) -> None:
    tid: str = f.parser_template(conn, name="approved_one", spec=json.dumps(SPEC))["id"]
    conn.commit()
    with db.write_session() as s:
        write_notice(s, s.get_one(ParserTemplate, tid), "approved",
                     received=["2026-09-02T04:00:00.000Z", "2026-08-01T04:00:00.000Z"])
    assert notices(db) == [("approved_one", "approved", "2026-08-01", "2026-09-02")]


def test_a_shipped_template_changed_on_startup_writes_a_notice(db: Database, conn: Connection, tmp_path: Path) -> None:
    (tmp_path / "example_debit.json").write_text(json.dumps(SPEC))
    (tmp_path / "patterns").mkdir()
    (tmp_path / "patterns" / "patterns.json").write_text(json.dumps(PATTERNS))
    with db.write_session() as s:
        sync_seeds(s, tmp_path, tmp_path / "patterns" / "patterns.json")
    read_by(conn, f.user(conn)["id"], "example_debit", "2026-10-01T04:00:00.000Z")
    conn.commit()
    with db.write_session() as s:
        sync_seeds(s, tmp_path, tmp_path / "patterns" / "patterns.json")  # unchanged: no notice
    assert notices(db) == []
    edited: dict[str, Any] = copy.deepcopy(SPEC)
    edited["match"]["subject"] = "^Debit alert v2"
    (tmp_path / "example_debit.json").write_text(json.dumps(edited))
    with db.write_session() as s:
        sync_seeds(s, tmp_path, tmp_path / "patterns" / "patterns.json")
    assert notices(db) == [("example_debit", "shipped_updated", "2026-10-01", "2026-10-01")]
