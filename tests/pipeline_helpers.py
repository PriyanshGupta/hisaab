"""Shared builders for the routing, pipeline and sync tests: synthetic emails and templates only."""

import hashlib
import json
import uuid
from typing import Any, TypeAlias

from sqlalchemy.orm import Session

from hisaab.db import Database
from hisaab.accounts import repository as accounts
from hisaab.models import Source, utc_now
from hisaab.parsing import templates
from hisaab.pipeline.derive import PipelineCounts, apply
from hisaab.pipeline.routing import Routed
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


def add_source(session: Session, user_id: str, content: bytes, message_id: str | None, kind: str = "email",
               **kw: Any) -> Source:
    source: Source = Source(
        ingest_seq=next_ingest_seq(session), user_id=user_id, kind=kind, content=content,
        content_hash=hashlib.sha256(content).hexdigest(), external_message_id=message_id,
        received_at="2026-10-05T20:00:00.000Z", ingested_at=utc_now(), processing_state="pending", **kw)
    session.add(source)
    session.flush()
    return source


# --- dedup and transfer tests: contracts straight into the pipeline, one run per ingest() call ---

DAY: str = "2026-09-01"  # well outside the rolling window of "today", unless a test wants it inside
BANK_EMAIL: str = "alerts@example-bank.in"
CARD_EMAIL: str = "cards@example-card.in"
OTHER_BANK_EMAIL: str = "alerts@other-bank.in"

Email: TypeAlias = tuple[dict[str, Any], str]  # (the §10.4 contract, the sender's address)


def contract(**kw: Any) -> dict[str, Any]:
    return {"outcome": "transaction", "amount": 50000, "currency": "INR", "direction": "debit",
            "account_identifier": "1111", "counterparty_identifier": None, "payee_raw": "Corner Shop",
            "reference_number": None, "date": DAY, "time": None, "balance": None, "missing_fields": []} | kw


def routed(extraction: dict[str, Any], sender: str) -> Routed:
    return Routed(extraction, "parsed", True, {"route": "tier1", "template": "t", "outcome": "transaction",
                                               "missing_fields": [], "attempts": [], "sender": sender}, None, None)


def ingest(db: Database, user_id: str, *emails: Email, kind: str = "email") -> tuple[PipelineCounts, list[str]]:
    """One pipeline run over new Sources; returns the counts and the Source ids, in order."""
    with db.write_session() as s:
        ids: list[str] = [add_source(s, user_id, json.dumps(c).encode() + uuid.uuid4().bytes,
                                     None if kind != "email" else uuid.uuid4().hex, kind=kind).id for c, _ in emails]
    return reingest(db, user_id, list(zip(ids, emails))), ids


def reingest(db: Database, user_id: str, sources: list[tuple[str, Email]]) -> PipelineCounts:
    """One pipeline run over Sources that already exist (a re-parse)."""
    with db.write_session() as s:
        return apply(s, user_id, [(i, routed(c, sender)) for i, (c, sender) in sources])


def make_accounts(db: Database, user_id: str) -> dict[str, str]:
    """Two banks (last4 1111, 2222) and a card (3333); name -> account id."""
    out: dict[str, str] = {}
    with db.write_session() as s:
        for name, kind, last4 in (("bank", "bank", "1111"), ("other_bank", "bank", "2222"),
                                  ("card", "credit_card", "3333")):
            out[name] = accounts.create_account(s, user_id, type=kind, display_name=name, opening_balance=0,
                                                opening_balance_date="2026-07-01", last4=last4).id
    return out
