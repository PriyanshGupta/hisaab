"""Shared builders for the routing, pipeline and sync tests: synthetic emails and templates only."""

import hashlib
from typing import Any

from sqlalchemy.orm import Session

from hisaab.db import Database
from hisaab.models import Source, utc_now
from hisaab.parsing import templates
from hisaab.transactions.repository import next_ingest_seq
from tests.gmail_fake import make_eml
from tests.test_email_interpreter import BODY, NOTICE_BODY
from tests.test_email_spec import NOTICE, SPEC

BANK: str = "alerts@example-bank.in"  # SPEC's sender, lower-cased
DEBIT_EML: bytes = make_eml(BANK, "Debit alert", BODY)
NOTICE_EML: bytes = make_eml(BANK, "Account update", NOTICE_BODY)
UNKNOWN_EML: bytes = make_eml("offers@shop.example", "Big sale", "<p>50% off everything this weekend</p>")


def install_templates(db: Database) -> None:
    with db.write_session() as s:
        templates.save_email_template(s, name="Example debit", spec=SPEC, status="active")
        templates.save_email_template(s, name="Example mandate notice", spec=NOTICE, status="active")


def add_source(session: Session, user_id: str, content: bytes, message_id: str, **kw: Any) -> Source:
    source: Source = Source(
        ingest_seq=next_ingest_seq(session), user_id=user_id, kind="email", content=content,
        content_hash=hashlib.sha256(content).hexdigest(), external_message_id=message_id,
        received_at="2026-10-05T20:00:00.000Z", ingested_at=utc_now(), processing_state="pending", **kw)
    session.add(source)
    session.flush()
    return source
