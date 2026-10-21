"""Approval (tech-design §4.10 step 5, PRD §4.8): the template goes live, every cached model reading of its
format is invalidated, the retained emails of that format are re-read at tier 1, the three examples are
exported as redacted fixtures, and stored content goes. Reject discards a candidate."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Connection, func, select

from hisaab.config import Settings
from hisaab.db import Database
from hisaab.jobs.queue import JobContext
from hisaab.models import ExtractionCache, FormatSighting, ParserTemplate, Source, Transaction, TransactionSource
from hisaab.models.views import TransactionEffective
from hisaab.promotion.promote import RETIER_REASON, ApproveResult, NotApprovable, approve, reject
from hisaab.promotion.propose import propose_ready
from tests import factories as f
from tests.claude_fake import FakeClaude
from tests.gmail_fake import make_eml
from tests.promotion_helpers import ALERT, LANTERN, PEOPLE, Haiku, alert_eml, feed, reading, sources
from tests.test_propose import ANSWER, WRONG


@pytest.fixture
def uid(conn: Connection) -> str:
    user_id: str = f.user(conn, display_name="Asha Rao")["id"]
    conn.commit()
    return user_id


@pytest.fixture
def cfg(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(update={"anthropic_api_key": SecretStr("test-key"), "fixtures_dir": tmp_path})


def proposed(db: Database, uid: str, cfg: Settings, answer: dict[str, Any] = ANSWER) -> str:
    propose_ready(JobContext(db, "job", uid, {}), cfg, FakeClaude(reply=lambda prompt: answer), lambda s: None)
    with db.read_session() as s:
        template_id: str | None = s.scalar(select(ParserTemplate.id).where(ParserTemplate.origin == "llm_proposed"))
    assert template_id is not None
    return template_id


def run_approve(db: Database, template_id: str, cfg: Settings) -> ApproveResult:
    with db.write_session() as s:
        return approve(s, template_id, cfg)


def cache_rows(db: Database) -> int:
    with db.read_session() as s:
        return s.scalar(select(func.count()).select_from(ExtractionCache)) or 0


def txn_of(db: Database, source_id: str) -> TransactionEffective:
    with db.read_session() as s:
        row: TransactionEffective = s.scalars(select(TransactionEffective).join(
            TransactionSource, TransactionSource.transaction_id == TransactionEffective.id)
            .where(TransactionSource.source_id == source_id)).one()
        s.expunge(row)
        return row


def test_approving_puts_the_template_live_and_rereads_its_format_at_tier_1(db: Database, uid: str,
                                                                           cfg: Settings) -> None:
    ids: list[str] = feed(db, uid, [alert_eml(n) for n in range(4)], Haiku(*[reading(n) for n in range(4)]))
    template_id: str = proposed(db, uid, cfg)
    assert cache_rows(db) == 4
    result: ApproveResult = run_approve(db, template_id, cfg)
    assert (result.reread, result.waiting, result.invalidated, result.flagged) == (4, 0, 4, 0)
    assert cache_rows(db) == 0  # without this, the cache would keep serving the model's old reading
    with db.read_session() as s:
        template: ParserTemplate = s.get_one(ParserTemplate, template_id)
        sighting: FormatSighting = s.scalars(select(FormatSighting)).one()
        assert (template.status, sighting.state) == ("active", "promoted") and template.promoted_at is not None
    for sid, src in sources(db).items():
        diagnostic: dict[str, Any] = json.loads(src.diagnostic or "{}")
        assert (diagnostic["route"], diagnostic["template"], src.content) == ("tier1", "lantern_upi_debit", None)
    assert txn_of(db, ids[2]).amount == 240000


def test_a_reread_keeps_the_users_edit_and_the_transaction(db: Database, conn: Connection, uid: str,
                                                           cfg: Settings) -> None:
    ids: list[str] = feed(db, uid, [alert_eml(n) for n in range(3)], Haiku(*[reading(n) for n in range(3)]))
    before: TransactionEffective = txn_of(db, ids[0])
    f.transaction_edit(conn, before.id, uid, field="transaction_date", new_value="2026-10-01",
                       prior_value="2026-10-05")
    conn.commit()
    run_approve(db, proposed(db, uid, cfg), cfg)
    after: TransactionEffective = txn_of(db, ids[0])
    assert (after.id, after.transaction_date) == (before.id, "2026-10-01")


def test_a_reread_that_disagrees_with_the_old_reading_goes_to_review(db: Database, uid: str, cfg: Settings) -> None:
    feed(db, uid, [alert_eml(n) for n in range(3)], Haiku(*[reading(n) for n in range(3)]))
    [odd] = feed(db, uid, [alert_eml(3)], Haiku({**reading(3), "amount": 3100}))  # the model misread this one
    result: ApproveResult = run_approve(db, proposed(db, uid, cfg), cfg)
    assert result.flagged == 1
    txn: TransactionEffective = txn_of(db, odd)
    assert txn.amount == 31000 and txn.status == "needs_review" and RETIER_REASON in (txn.review_reason or "")


def test_an_email_the_template_declines_waits_for_the_model_again(db: Database, uid: str, cfg: Settings) -> None:
    feed(db, uid, [alert_eml(n) for n in range(3)], Haiku(*[reading(n) for n in range(3)]))
    whole_rupees: bytes = make_eml(LANTERN, "You have done a UPI txn",
                                   ALERT.format(verb="debited", **{**PEOPLE[3], "amount": "310"}))
    [odd] = feed(db, uid, [whole_rupees], Haiku(reading(3)))
    result: ApproveResult = run_approve(db, proposed(db, uid, cfg), cfg)
    assert (result.reread, result.waiting) == (3, 1)
    src: Source = sources(db)[odd]
    assert src.processing_state == "pending_llm" and src.content is not None


def test_every_cached_reading_of_the_format_goes_and_other_formats_stay(db: Database, conn: Connection, uid: str,
                                                                        cfg: Settings) -> None:
    feed(db, uid, [alert_eml(n) for n in range(3)], Haiku(*[reading(n) for n in range(3)]))
    with db.read_session() as s:
        value: str = s.scalars(select(FormatSighting.fingerprint)).one()
    gone: str = f.source(conn, uid, diagnostic=json.dumps({"route": "tier2", "fingerprint": value}))["content_hash"]
    f.extraction_cache(conn, content_hash=gone)
    kept: str = f.extraction_cache(conn)["content_hash"]  # another format's reading
    conn.commit()
    result: ApproveResult = run_approve(db, proposed(db, uid, cfg), cfg)
    assert result.invalidated == 4
    with db.read_session() as s:
        assert s.get(ExtractionCache, gone) is None and s.get(ExtractionCache, kept) is not None


def test_the_three_examples_are_exported_redacted(db: Database, uid: str, cfg: Settings, tmp_path: Path) -> None:
    feed(db, uid, [alert_eml(n) for n in range(3)], Haiku(*[reading(n) for n in range(3)]))
    (tmp_path / "promoted").mkdir()
    (tmp_path / "promoted" / "lantern_upi_debit_01.eml").write_bytes(b"an older export")
    result: ApproveResult = run_approve(db, proposed(db, uid, cfg), cfg)
    assert result.exported == [f"promoted/lantern_upi_debit_0{n}.eml" for n in (2, 3, 4)]
    assert (tmp_path / "promoted" / "lantern_upi_debit_01.eml").read_bytes() == b"an older export"
    for name in result.exported:
        eml: bytes = (tmp_path / name).read_bytes()
        expected: dict[str, Any] = json.loads((tmp_path / name).with_suffix(".expected.json").read_text())
        for secret in ("Corner Grocer", "RAVI KUMAR", "Lamp House", "612345678901", "Asha", "asha@example.test"):
            assert secret.encode() not in eml and secret not in json.dumps(expected), secret
        assert expected["outcome"] == "transaction" and "missing_fields" in expected


def test_each_users_emails_are_reread_in_their_own_pipeline(db: Database, conn: Connection, uid: str,
                                                            cfg: Settings) -> None:
    other: str = f.user(conn, display_name="Vikram Shah")["id"]
    conn.commit()
    feed(db, uid, [alert_eml(n) for n in range(3)], Haiku(*[reading(n) for n in range(3)]))
    [theirs] = feed(db, other, [alert_eml(3)], Haiku(reading(3)))
    run_approve(db, proposed(db, uid, cfg), cfg)
    txn: TransactionEffective = txn_of(db, theirs)
    assert txn.user_id == other and json.loads(sources(db)[theirs].diagnostic or "{}")["route"] == "tier1"
    with db.read_session() as s:
        assert s.scalar(select(func.count()).select_from(Transaction).where(Transaction.user_id == other)) == 1


def test_a_candidate_that_failed_replay_cannot_be_approved(db: Database, uid: str, cfg: Settings) -> None:
    feed(db, uid, [alert_eml(n) for n in range(3)], Haiku(*[reading(n) for n in range(3)]))
    template_id: str = proposed(db, uid, cfg, WRONG)
    with pytest.raises(NotApprovable):
        run_approve(db, template_id, cfg)


def test_reject_discards_the_candidate_and_the_same_examples_are_not_sent_again(db: Database, uid: str,
                                                                                 cfg: Settings) -> None:
    feed(db, uid, [alert_eml(n) for n in range(3)], Haiku(*[reading(n) for n in range(3)]))
    template_id: str = proposed(db, uid, cfg)
    with db.write_session() as s:
        reject(s, template_id)
    with db.read_session() as s:
        template: ParserTemplate = s.get_one(ParserTemplate, template_id)
        sighting: FormatSighting = s.scalars(select(FormatSighting)).one()
        assert (template.status, sighting.state) == ("proposed", "accumulating")
        assert json.loads(template.validation_result or "{}")["passed"] is False
    fake: FakeClaude = FakeClaude(reply=lambda prompt: ANSWER)
    propose_ready(JobContext(db, "job", uid, {}), cfg, fake, lambda s: None)
    assert fake.calls == 0
    with db.write_session() as s, pytest.raises(NotApprovable):
        approve(s, template_id, cfg)


def test_auto_promotion_approves_a_passing_candidate_with_nobody_clicking(db: Database, uid: str,
                                                                          cfg: Settings) -> None:
    feed(db, uid, [alert_eml(n) for n in range(3)], Haiku(*[reading(n) for n in range(3)]))
    auto: Settings = cfg.model_copy(update={"auto_promote_templates": True})
    counts: dict[str, int] = propose_ready(JobContext(db, "job", uid, {}), auto,
                                           FakeClaude(reply=lambda prompt: ANSWER), lambda s: None)
    assert counts["auto_promoted"] == 1 and cache_rows(db) == 0
    with db.read_session() as s:
        assert s.scalar(select(ParserTemplate.status).where(ParserTemplate.origin == "llm_proposed")) == "active"


def test_auto_promotion_is_off_by_default(settings: Settings) -> None:
    assert settings.auto_promote_templates is False


def test_approval_writes_a_resync_notice_covering_the_formats_emails(db: Database, uid: str, cfg: Settings) -> None:
    """Slice 24: the format's emails whose cached readings approval deleted need a re-sync to be read by it."""
    from hisaab.models import ResyncNotice
    feed(db, uid, [alert_eml(n) for n in range(3)], Haiku(*[reading(n) for n in range(3)]))
    run_approve(db, proposed(db, uid, cfg), cfg)
    with db.read_session() as s:
        [notice] = s.scalars(select(ResyncNotice)).all()
        assert (notice.template_name, notice.reason) == ("lantern_upi_debit", "approved")
        assert notice.range_from == notice.range_to == "2026-10-06"  # add_source's received_at, in India
