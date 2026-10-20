"""Reading an uploaded statement and staging it for the preview (tech-design §4.7, §7 steps 1-4).

Synthetic statements only (tests/statement_samples.py): the RULED spec reads
"Account No : 123456781234", so the bank account here ends 1234.
"""

import json
from typing import Any
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import Connection

from hisaab.accounts import repository as accounts
from hisaab.config import Settings
from hisaab.db import Database
from hisaab.models import StatementStaging, utc_now
from hisaab.parsing import templates
from hisaab.statements.reading import ReadStatement, StatementError, read_statement
from hisaab.statements.staging import load, rebind, rows_of, stage
from tests import factories as f
from tests.claude_fake import FakeClaude
from tests.pdf_factory import make_pdf
from tests.pipeline_helpers import contract, ingest
from tests.statement_samples import ruled_page, ruled_pdf
from tests.test_statement_spec import LINES, RULED

DAY: str = "2026-09-01"
ROW: tuple[str, ...] = ("01/09/26", "CORNER SHOP", "", "100.00", "", "900.00")
SUMMARY: list[str] = ["STATEMENT SUMMARY", "Opening 1,000.00", "Closing 900.00"]
PASSING: bytes = ruled_pdf(ruled_page([ROW], first=True, summary=SUMMARY))


@pytest.fixture
def user_id(db: Database, conn: Connection) -> str:
    uid: str = f.user(conn)["id"]
    conn.commit()
    with db.write_session() as s:
        templates.save_statement_template(s, name="Example statement", spec=RULED, status="active")
        templates.save_statement_template(s, name="Example card statement", spec=LINES, status="active")
    return uid


def make_account(db: Database, user_id: str, last4: str, kind: str = "bank") -> str:
    with db.write_session() as s:
        return accounts.create_account(s, user_id, type=kind, display_name=f"{kind} {last4}", last4=last4).id


@pytest.fixture
def bank(db: Database, user_id: str) -> str:
    return make_account(db, user_id, "1234")


NO_MODEL: Settings = Settings(_env_file=None, anthropic_api_key=None)  # a known layout must never need the model


def read(db: Database, user_id: str, data: bytes, password: str | None = None, *, settings: Settings = NO_MODEL,
         claude: FakeClaude | None = None) -> ReadStatement:
    with db.read_session() as s:
        return read_statement(s, user_id, data, password, settings=settings, transport=claude or FakeClaude(),
                              sleep=lambda _s: None)


def staged(db: Database, user_id: str, data: bytes = PASSING) -> str:
    found: ReadStatement = read(db, user_id, data)
    with db.write_session() as s:
        return stage(s, user_id, "statement.pdf", "hash-1", found).id


def test_reads_binds_and_validates(db: Database, user_id: str, bank: str) -> None:
    found: ReadStatement = read(db, user_id, PASSING)
    assert found.account_id == bank and found.template is not None and found.template.name == "Example statement"
    assert found.result.validation["outcome"] == "passed"
    assert [(r.date, r.direction, r.amount) for r in found.result.rows] == [(DAY, "debit", 10000)]


def test_an_encrypted_statement_opens_with_its_password(db: Database, user_id: str, bank: str) -> None:
    data: bytes = ruled_pdf(ruled_page([ROW], first=True, summary=SUMMARY), password="open sesame")
    assert read(db, user_id, data, "open sesame").result.validation["outcome"] == "passed"


@pytest.mark.parametrize(("data", "password", "says"), [
    (b"not a pdf at all", None, "isn't a PDF"),
    (ruled_pdf(ruled_page([ROW], first=True), password="pw"), None, "password-protected"),
    (ruled_pdf(ruled_page([ROW], first=True), password="pw"), "wrong", "doesn't open"),
    (make_pdf([{"texts": []}]), None, "no text"),
])
def test_refusals_are_sentences(db: Database, user_id: str, data: bytes, password: str | None, says: str) -> None:
    with pytest.raises(StatementError, match=says) as e:
        read(db, user_id, data, password)
    assert "wrong" not in str(e.value) or says == "doesn't open"  # the password is never echoed


def test_unbound_when_the_number_is_not_one_of_the_users_accounts(db: Database, user_id: str) -> None:
    make_account(db, user_id, "9999")
    assert read(db, user_id, PASSING).account_id is None


def test_a_bank_number_on_a_card_statement_is_not_bound(db: Database, user_id: str) -> None:
    make_account(db, user_id, "1234", kind="credit_card")  # the card's last 4 equal the statement's account
    assert read(db, user_id, PASSING).account_id is None


def test_stage_forecasts_matches_and_new_rows(db: Database, user_id: str, bank: str) -> None:
    ingest(db, user_id, (contract(amount=10000, account_identifier="1234", date="2026-09-02"), "alerts@x.in"))
    second: tuple[str, ...] = ("03/09/26", "BOOKS", "", "50.00", "", "850.00")
    staging_id: str = staged(db, user_id, ruled_pdf(ruled_page([ROW, second], first=True, summary=SUMMARY)))
    with db.read_session() as s:
        row: StatementStaging | None = load(s, user_id, staging_id)
        assert row is not None
        dispositions: list[str] = json.loads(row.row_dispositions)
        assert dispositions[0].startswith("matches:") and dispositions[1] == "new"
        assert [r.narration for r in rows_of(row)] == ["CORNER SHOP", "BOOKS"]
        assert (row.declared_opening, row.declared_closing, row.account_id) == (100000, 90000, bank)
        assert row.expires_at > utc_now(datetime.now(UTC) + timedelta(hours=23))


def test_two_identical_rows_never_forecast_the_same_transaction(db: Database, user_id: str, bank: str) -> None:
    ingest(db, user_id, (contract(amount=10000, account_identifier="1234", date=DAY), "alerts@x.in"))
    staging_id: str = staged(db, user_id, ruled_pdf(ruled_page([ROW, ROW], first=True)))
    with db.read_session() as s:
        row: StatementStaging | None = load(s, user_id, staging_id)
        assert row is not None
        dispositions: list[str] = json.loads(row.row_dispositions)
    assert dispositions[0].startswith("matches:") and dispositions[1] == "new"


def test_unbound_rows_are_all_new_until_an_account_is_picked(db: Database, user_id: str) -> None:
    other: str = make_account(db, user_id, "9999")
    ingest(db, user_id, (contract(amount=10000, account_identifier="9999", date=DAY), "alerts@x.in"))
    staging_id: str = staged(db, user_id)
    with db.read_session() as s:
        row: StatementStaging | None = load(s, user_id, staging_id)
        assert row is not None and row.account_id is None and json.loads(row.row_dispositions) == ["new"]
    with db.write_session() as s:
        picked: StatementStaging = rebind(s, user_id, staging_id, other)
        assert picked.account_id == other and json.loads(picked.row_dispositions)[0].startswith("matches:")


def test_rebind_refuses_a_card_for_a_bank_statement_an_archived_account_or_someone_elses(
        db: Database, conn: Connection, user_id: str) -> None:
    staging_id: str = staged(db, user_id)
    card: str = make_account(db, user_id, "5555", kind="credit_card")
    archived: str = make_account(db, user_id, "6666")
    with db.write_session() as s:
        accounts.archive_account(s, user_id, archived)
    stranger: str = f.user(conn)["id"]
    conn.commit()
    theirs: str = make_account(db, stranger, "7777")
    for account_id, says in ((card, "bank account"), (archived, "archived"), (theirs, "No such account")):
        with pytest.raises(StatementError, match=says), db.write_session() as s:
            rebind(s, user_id, staging_id, account_id)


def test_load_hides_expired_and_other_users_previews(db: Database, conn: Connection, user_id: str, bank: str) -> None:
    staging_id: str = staged(db, user_id)
    stranger: str = f.user(conn)["id"]
    conn.commit()
    with db.read_session() as s:
        assert load(s, stranger, staging_id) is None and load(s, user_id, staging_id) is not None
    with db.write_session() as s:
        s.get_one(StatementStaging, staging_id).expires_at = utc_now(datetime.now(UTC) - timedelta(seconds=1))
    with db.read_session() as s:
        assert load(s, user_id, staging_id) is None  # expired but not yet swept: gone all the same


# --- tier 3: layouts no template reads (ruling 2026-10-19) ---------------------------------------------

UNKNOWN: bytes = make_pdf([{"texts": [(40, 30, "Some Other Bank"), (40, 45, "Savings account ending 1234"),
                                      (40, 60, "Opening 1,000.00"), (40, 75, "Closing 900.00"),
                                      (40, 100, "01/09/26"), (120, 100, "CORNER SHOP"), (400, 100, "100.00")]}])
MODEL_ANSWER: dict[str, Any] = {
    "outcome": "statement", "account_kind": "bank", "account_last4": "1234", "institution": "Some Other Bank",
    "opening": 100000, "closing": 90000, "unreadable": [],
    "rows": [{"page": 1, "date": DAY, "time": None, "narration": "CORNER SHOP", "reference": None,
              "direction": "debit", "amount": 10000, "balance": None, "bill_payment": False}],
}
WITH_MODEL: Settings = Settings(_env_file=None, anthropic_api_key=SecretStr("sk-test"))


def test_an_unknown_layout_is_read_by_the_model(db: Database, user_id: str, bank: str) -> None:
    claude: FakeClaude = FakeClaude(reply=lambda prompt: MODEL_ANSWER)
    found: ReadStatement = read(db, user_id, UNKNOWN, settings=WITH_MODEL, claude=claude)
    assert claude.calls == 1 and "CORNER SHOP" in claude.requests[0]["messages"][0]["content"]
    assert found.template is None and (found.account_id, found.account_kind) == (bank, "bank")
    assert (found.result.validation["tier"], found.result.validation["outcome"]) == (3, "passed")
    with db.write_session() as s:
        row: StatementStaging = stage(s, user_id, "other.pdf", "hash-9", found)
        assert row.template_id is None and json.loads(row.validation_result)["account_kind"] == "bank"


def test_a_known_layout_never_calls_the_model(db: Database, user_id: str, bank: str) -> None:
    claude: FakeClaude = FakeClaude(reply=lambda prompt: MODEL_ANSWER)
    assert read(db, user_id, PASSING, settings=WITH_MODEL, claude=claude).template is not None
    assert claude.calls == 0


def test_a_template_that_does_not_fit_falls_through_and_says_which(db: Database, user_id: str, bank: str) -> None:
    labels: list[str] = ["Date", "Narration", "Ref", "Deposit", "Withdrawal", "Balance"]
    misfit: bytes = ruled_pdf(ruled_page([ROW], first=True, labels=labels, summary=SUMMARY))
    answer: dict[str, Any] = MODEL_ANSWER | {"opening": None, "closing": None}
    found: ReadStatement = read(db, user_id, misfit, settings=WITH_MODEL, claude=FakeClaude(reply=lambda p: answer))
    tried: dict[str, str] = found.result.validation["template_tried"]
    assert tried["template"] == "Example statement" and "Withdrawal" in tried["reason"]


def test_without_the_model_an_unknown_layout_is_a_sentence(db: Database, user_id: str) -> None:
    with pytest.raises(StatementError, match="isn't one Hisaab knows, and the model that reads new layouts isn't "
                                             "available right now"):
        read(db, user_id, UNKNOWN)  # no API key configured


def test_a_model_answer_that_breaks_the_contract_is_a_sentence(db: Database, user_id: str) -> None:
    claude: FakeClaude = FakeClaude(reply=lambda prompt: MODEL_ANSWER | {"rows": "lots"})
    with pytest.raises(StatementError, match="The model's reading of this statement couldn't be used.*Nothing was saved"):
        read(db, user_id, UNKNOWN, settings=WITH_MODEL, claude=claude)


def test_a_file_the_model_says_is_not_a_statement_is_a_sentence(db: Database, user_id: str) -> None:
    claude: FakeClaude = FakeClaude(reply=lambda prompt: MODEL_ANSWER | {"outcome": "not_a_statement", "rows": []})
    with pytest.raises(StatementError, match="doesn't look like a bank or card statement"):
        read(db, user_id, UNKNOWN, settings=WITH_MODEL, claude=claude)
