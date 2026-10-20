"""Invented emails of a format no template knows (Lantern Bank), and Haiku stand-ins that read them.

Every value here is made up. The emails go through the real routing and pipeline; only the model is faked.
"""

import uuid
from collections.abc import Iterator
from typing import Any

from sqlalchemy import select

from hisaab.db import Database
from hisaab.models import Source
from hisaab.parsing.email_interpreter import EmailArtifact
from hisaab.pipeline.derive import PipelineCounts, apply
from hisaab.pipeline.routing import Routed, route
from tests.gmail_fake import make_eml
from tests.pipeline_helpers import add_source

LANTERN: str = "alerts@lantern.example"
MODEL: str = "claude-haiku-5-5"
ALERT: str = """<html><body><table><tr><td><p>Dear {who},</p>
<p>Rs.{amount} has been {verb} from account **{last4} to VPA {vpa} {payee} on {day}.</p>
<p>Your UPI transaction reference number is {ref}.</p>
<p>If you did not authorise this, please call us at once.</p></td></tr></table></body></html>"""
OFFER: str = """<html><body><div><p>Dear {who},</p><p>Enjoy {pct}% cashback on your next {n} purchases.</p>
<p>Offer valid till {day}. Terms and conditions apply.</p></div></body></html>"""

PEOPLE: list[dict[str, str]] = [
    {"amount": "1,250.00", "last4": "4321", "vpa": "corner@okbank", "payee": "Corner Grocer", "day": "05-10-26",
     "ref": "612345678901", "who": "Asha Rao"},
    {"amount": "89.50", "last4": "4321", "vpa": "ravi.k@upibank", "payee": "RAVI KUMAR", "day": "07-10-26",
     "ref": "698765432109", "who": "Asha Rao"},
    {"amount": "2,400.00", "last4": "4321", "vpa": "lamp@okbank", "payee": "Lamp House", "day": "09-10-26",
     "ref": "655544433322", "who": "Asha Rao"},
    {"amount": "310.00", "last4": "4321", "vpa": "tea@upibank", "payee": "Tea Stall", "day": "11-10-26",
     "ref": "677788899900", "who": "Asha Rao"},
]


def alert_eml(n: int, verb: str = "debited") -> bytes:
    p: dict[str, str] = PEOPLE[n]
    return make_eml(LANTERN, "You have done a UPI txn", ALERT.format(verb=verb, **p))


def offer_eml(n: int) -> bytes:
    return make_eml(LANTERN, f"Offer {n}", OFFER.format(who="Asha Rao", pct=5 + n, n=3 + n, day=f"1{n}-11-26"))


def reading(n: int, verb: str = "debited") -> dict[str, Any]:
    """What Haiku would say about alert_eml(n): the §10.4 contract."""
    p: dict[str, str] = PEOPLE[n]
    d, m, y = p["day"].split("-")
    return {"outcome": "transaction", "amount": int(p["amount"].replace(",", "").replace(".", "")), "currency": "INR",
            "direction": "debit" if verb == "debited" else "credit", "account_identifier": p["last4"],
            "counterparty_identifier": p["vpa"], "payee_raw": p["payee"], "reference_number": p["ref"],
            "date": f"20{y}-{m}-{d}", "time": None, "balance": None, "missing_fields": []}


MARKETING_READING: dict[str, Any] = {"outcome": "non_transactional", "amount": None, "currency": None,
                                     "direction": None, "account_identifier": None, "counterparty_identifier": None,
                                     "payee_raw": None, "reference_number": None, "date": None, "time": None,
                                     "balance": None, "missing_fields": []}


class Haiku:
    """Answers in turn from `replies`; counts calls. An exception in the list is raised."""

    def __init__(self, *replies: dict[str, Any] | Exception) -> None:
        self.replies: Iterator[dict[str, Any] | Exception] = iter(replies)
        self.calls: int = 0

    def __call__(self, artifact: EmailArtifact) -> dict[str, Any]:
        self.calls += 1
        reply: dict[str, Any] | Exception = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


def feed(db: Database, user_id: str, emls: list[bytes], llm: Haiku) -> list[str]:
    """Store the emails as new Sources and run them through routing and the pipeline, one batch."""
    with db.write_session() as s:
        ids: list[str] = [add_source(s, user_id, c, uuid.uuid4().hex).id for c in emls]
    rerun(db, user_id, ids, llm)
    return ids


def rerun(db: Database, user_id: str, ids: list[str], llm: Haiku) -> PipelineCounts:
    with db.read_session() as s:
        batch: list[tuple[str, Routed]] = [(i, route(s, s.get_one(Source, i), llm, MODEL)) for i in ids]
    with db.write_session() as s:
        return apply(s, user_id, batch)


def sources(db: Database) -> dict[str, Source]:
    with db.read_session() as s:
        rows: list[Source] = list(s.scalars(select(Source)))
        for r in rows:
            s.expunge(r)
        return {r.id: r for r in rows}
