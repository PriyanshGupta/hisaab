"""Review status from effective values (tech-design §3.5, §6.6; slice 17).

A user's correction answers the question that put a transaction in review, so
status is recomputed from the overlay, by the user's action and by every later
pipeline run alike. "Looks right" (review_acknowledged = the amount) silences
the guardrails until the amount changes.
"""

import pytest
from sqlalchemy import Connection, select

from hisaab.db import Database
from hisaab.models import NormalizationPattern, Transaction, TransactionSource
from hisaab.payees import repository as payees
from hisaab.transactions import repository as txns
from hisaab.transactions import review
from tests import factories as f
from tests.pipeline_helpers import BANK_EMAIL, contract, ingest, make_accounts, reingest


@pytest.fixture
def user_id(db: Database, conn: Connection) -> str:
    uid: str = f.user(conn)["id"]
    conn.commit()
    return uid


@pytest.fixture
def bank(db: Database, user_id: str) -> str:
    return make_accounts(db, user_id)["bank"]


def known_payee(db: Database, name: str = "Corner Shop") -> str:
    with db.write_session() as s:
        payee_id: str = payees.create_payee(s, name).id
        s.add(NormalizationPattern(match_type="exact", pattern=name, payee_id=payee_id, origin="seed"))
        return payee_id


def state(db: Database, txn_id: str) -> tuple[str, str | None]:
    with db.read_session() as s:
        t: Transaction = s.get_one(Transaction, txn_id)
        return t.status, t.review_reason


def one_txn(db: Database, source_id: str) -> str:
    with db.read_session() as s:
        txn_id: str = s.scalars(select(TransactionSource.transaction_id).where(
            TransactionSource.source_id == source_id)).one()
        return txn_id


def edit(db: Database, user_id: str, txn_id: str, **changes: object) -> None:
    with db.write_session() as s:
        txns.apply_edits(s, user_id, txn_id, changes)


def test_naming_the_payee_by_edit_takes_it_out_of_review(db: Database, user_id: str, bank: str) -> None:
    _, [src] = ingest(db, user_id, (contract(), BANK_EMAIL))
    txn: str = one_txn(db, src)
    assert state(db, txn) == ("needs_review", "payee not recognised")
    edit(db, user_id, txn, payee_id=known_payee(db, "Someone Else"))  # a different payee: no pattern matches
    assert state(db, txn) == ("confirmed", None)


def test_a_re_parse_after_the_edit_keeps_it_confirmed(db: Database, user_id: str, bank: str) -> None:
    email: tuple[dict[str, object], str] = (contract(), BANK_EMAIL)
    _, [src] = ingest(db, user_id, email)
    txn: str = one_txn(db, src)
    edit(db, user_id, txn, payee_id=known_payee(db, "Someone Else"))
    reingest(db, user_id, [(src, email)])  # the pipeline agrees with the user's action
    assert state(db, txn) == ("confirmed", None)


def test_setting_the_account_by_edit_clears_the_unresolved_reason(db: Database, user_id: str, bank: str) -> None:
    known_payee(db)
    _, [src] = ingest(db, user_id, (contract(account_identifier="4821"), BANK_EMAIL))
    txn: str = one_txn(db, src)
    assert state(db, txn) == ("needs_review", "account 4821 isn't one of yours yet")
    edit(db, user_id, txn, from_account_id=bank)
    assert state(db, txn) == ("confirmed", None)


def test_looks_right_quiets_a_large_amount_until_the_amount_changes(db: Database, user_id: str, bank: str) -> None:
    known_payee(db)
    _, [src] = ingest(db, user_id, (contract(amount=3_000_000), BANK_EMAIL))
    txn: str = one_txn(db, src)
    assert state(db, txn) == ("needs_review", "amount over ₹25,000")
    with db.write_session() as s:
        txns.acknowledge(s, user_id, txn)
    assert state(db, txn) == ("confirmed", None)
    edit(db, user_id, txn, amount=2_600_000)
    assert state(db, txn) == ("needs_review", "amount over ₹25,000")


def test_looks_right_never_answers_which_account_it_was(db: Database, user_id: str, bank: str) -> None:
    known_payee(db)
    _, [src] = ingest(db, user_id, (contract(amount=3_000_000, account_identifier="4821"), BANK_EMAIL))
    txn: str = one_txn(db, src)
    with db.write_session() as s:
        txns.acknowledge(s, user_id, txn)  # silences the guardrail, not the missing account
    assert state(db, txn) == ("needs_review", "account 4821 isn't one of yours yet")


def test_a_re_parse_after_looks_right_keeps_it_quiet(db: Database, user_id: str, bank: str) -> None:
    known_payee(db)
    email: tuple[dict[str, object], str] = (contract(amount=3_000_000), BANK_EMAIL)
    _, [src] = ingest(db, user_id, email)
    txn: str = one_txn(db, src)
    with db.write_session() as s:
        txns.acknowledge(s, user_id, txn)
    reingest(db, user_id, [(src, email)])
    assert state(db, txn) == ("confirmed", None)


def test_an_edited_field_no_longer_counts_as_unread(db: Database, user_id: str, bank: str) -> None:
    known_payee(db)
    _, [src] = ingest(db, user_id, (contract(missing_fields=["amount", "date"]), BANK_EMAIL))
    txn: str = one_txn(db, src)
    assert state(db, txn) == ("needs_review", "couldn't read: amount, date")
    edit(db, user_id, txn, amount=51000)  # the user sets the figure
    assert state(db, txn) == ("needs_review", "couldn't read: date")


def test_a_possible_transfer_doubt_survives_an_unrelated_correction(db: Database, user_id: str, bank: str,
                                                                    conn: Connection) -> None:
    known_payee(db)
    _, [src] = ingest(db, user_id, (contract(), BANK_EMAIL))
    txn: str = one_txn(db, src)
    with db.write_session() as s:  # only pairing can raise this doubt; a correction must not erase it
        s.get_one(Transaction, txn).status = "needs_review"
        s.get_one(Transaction, txn).review_reason = "possible transfer: 2 matching transactions"
    category: str = f.category(conn, user_id)["id"]
    conn.commit()
    edit(db, user_id, txn, category_override_id=category)
    assert state(db, txn) == ("needs_review", "possible transfer: 2 matching transactions")


def test_marking_it_a_transfer_answers_the_doubt(db: Database, user_id: str, conn: Connection) -> None:
    accounts: dict[str, str] = make_accounts(db, user_id)
    known_payee(db)
    _, [src] = ingest(db, user_id, (contract(), BANK_EMAIL))
    txn: str = one_txn(db, src)
    with db.write_session() as s:
        s.get_one(Transaction, txn).review_reason = "possible transfer: 2 matching transactions"
    edit(db, user_id, txn, kind="transfer", to_account_id=accounts["other_bank"])
    assert state(db, txn) == ("confirmed", None)


@pytest.mark.parametrize(("reason", "tag"), [
    ("couldn't read: payee_raw", "parse"),
    ("couldn't tell money in from money out", "parse"),
    ("currency USD isn't supported (INR only)", "parse"),
    ("account 4821 isn't one of yours yet", "account"),
    ("no account named in the email", "account"),
    ("payee not recognised", "payee"),
    ("no payee in the email", "payee"),
    ("amount over ₹25,000", "large"),
    ("possible transfer: 2 matching transactions", "transfer"),
    ("possible transfer: matches a transaction you've edited", "transfer"),
    (review.CARD_PAYMENT, "transfer"),
    ("the statement disagrees with your amount", "statement"),
])
def test_every_reason_the_pipeline_writes_has_a_tag(reason: str, tag: str) -> None:
    assert review.tag_of(reason) == tag
