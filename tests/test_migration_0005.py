"""Migration 0005: sources.attachments, attachment METADATA only (ruling 2026-10-11)."""

import json
from typing import Any

from sqlalchemy import Connection, text

from tests import factories as f


def test_sources_carry_attachment_metadata(conn: Connection) -> None:
    meta: list[dict[str, Any]] = [{"filename": "statement.pdf", "size": 48213, "mime_type": "application/pdf"}]
    src: dict[str, Any] = f.source(conn, attachments=json.dumps(meta))
    stored: str = conn.execute(text("SELECT attachments FROM sources WHERE id = :id"), {"id": src["id"]}).scalar_one()
    assert json.loads(stored) == meta


def test_attachments_default_to_null(conn: Connection) -> None:
    src: dict[str, Any] = f.source(conn)
    assert conn.execute(text("SELECT attachments FROM sources WHERE id = :id"), {"id": src["id"]}).scalar_one() is None
