"""Transfer detection (tech-design §6.3, rulings 2026-10-11).

A transfer is money moving between two accounts the same user owns. Either
condition suffices: a debit and a credit on two different own accounts, same
amount, within ±5 days (one transaction, both Sources linked); or the email
names one of the user's own accounts as the other side. A credit into a card
naming no merchant is a card bill payment, its paying account unknown until a
bank debit turns up. Detection re-examines the last 5 days of unmatched debits
every run, so a debit whose credit arrives a run later is still reclassified.
"""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, func, select

from hisaab.accounts import repository as accounts
from hisaab.clock import local_today
from hisaab.config import PROJECT_ROOT
from hisaab.db import Database
from hisaab.models import Source, Transaction
from hisaab.models.views import TransactionEffective
from hisaab.pipeline import derive
from hisaab.pipeline.derive import apply
from hisaab.pipeline.routing import Routed, route
from hisaab.seeds import sync_seeds
from hisaab.transactions import repository as txns
from tests import factories as f
from tests.pipeline_helpers import (
    BANK_EMAIL,
    CARD_EMAIL,
    OTHER_BANK_EMAIL,
    add_source,
    contract,
    ingest,
    install_templates,
    make_accounts,
    reingest,
)
from tests.test_dedup import transactions
from tests.test_routing import MODEL, Llm

CARDS: Path = PROJECT_ROOT / "fixtures" / "emails" / "credit_cards"
DEBIT: dict[str, Any] = contract(account_identifier="1111", payee_raw="Self", reference_number=None)
CREDIT: dict[str, Any] = contract(direction="credit", account_identifier="2222", payee_raw="Self")


@pytest.fixture
def user_id(db: Database, conn: Connection) -> str:
    uid: str = f.user(conn)["id"]
    conn.commit()
    install_templates(db)
    return uid


@pytest.fixture
def acct(db: Database, user_id: str) -> dict[str, str]:
    return make_accounts(db, user_id)


def recent(days_ago: int) -> str:
    return (local_today() - timedelta(days=days_ago)).isoformat()


def test_a_transfer_between_two_own_banks_is_one_transfer_with_both_legs(db: Database, user_id: str,
                                                                         acct: dict[str, str]) -> None:
    _, ids = ingest(db, user_id, (DEBIT, BANK_EMAIL), (CREDIT, OTHER_BANK_EMAIL))
    [txn] = transactions(db)
    assert (txn["kind"], txn["from"], txn["to"], txn["sources"]) == \
        ("transfer", acct["bank"], acct["other_bank"], sorted(ids))


def test_a_credit_in_a_later_run_reclassifies_the_debit_as_a_transfer(db: Database, user_id: str,
                                                                      acct: dict[str, str]) -> None:
    ingest(db, user_id, (DEBIT, BANK_EMAIL))
    [before] = transactions(db)
    assert before["kind"] == "expense"
    ingest(db, user_id, (CREDIT | {"date": "2026-09-04"}, OTHER_BANK_EMAIL))  # three days later, a later run
    [after] = transactions(db)
    assert (after["id"], after["kind"], after["from"], after["to"]) == \
        (before["id"], "transfer", acct["bank"], acct["other_bank"])
    with db.read_session() as s:
        assert s.get_one(Transaction, after["id"]).derived_key == s.get_one(Transaction, before["id"]).derived_key


def test_the_credit_first_then_the_debit_anchors_on_the_credit(db: Database, user_id: str,
                                                               acct: dict[str, str]) -> None:
    _, [credit] = ingest(db, user_id, (CREDIT, OTHER_BANK_EMAIL))
    [income] = transactions(db)
    ingest(db, user_id, (DEBIT, BANK_EMAIL))
    [txn] = transactions(db)
    assert (txn["id"], txn["kind"], txn["primary"]) == (income["id"], "transfer", credit)


def test_the_rolling_window_pairs_two_rows_that_already_exist(db: Database, user_id: str, acct: dict[str, str],
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as m:  # as if both had arrived before pairing could see them
        m.setattr(derive, "_pair", lambda run, txn: None)
        ingest(db, user_id, (DEBIT | {"date": recent(2)}, BANK_EMAIL), (CREDIT | {"date": recent(1)}, OTHER_BANK_EMAIL))
    assert [t["kind"] for t in transactions(db)] == ["expense", "income"]
    ingest(db, user_id, (contract(amount=100, account_identifier="3333"), CARD_EMAIL))  # an unrelated run
    kinds: list[str] = sorted(t["kind"] for t in transactions(db))
    assert kinds == ["expense", "transfer"]  # the unrelated purchase, and the pair


def test_a_debit_older_than_the_window_is_not_re_examined(db: Database, user_id: str, acct: dict[str, str],
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as m:
        m.setattr(derive, "_pair", lambda run, txn: None)
        ingest(db, user_id, (DEBIT | {"date": recent(9)}, BANK_EMAIL), (CREDIT | {"date": recent(8)}, OTHER_BANK_EMAIL))
    ingest(db, user_id, (contract(amount=100, account_identifier="3333"), CARD_EMAIL))
    assert sorted(t["kind"] for t in transactions(db)) == ["expense", "expense", "income"]


def test_outside_five_days_is_no_pair(db: Database, user_id: str, acct: dict[str, str]) -> None:
    ingest(db, user_id, (DEBIT, BANK_EMAIL), (CREDIT | {"date": "2026-09-07"}, OTHER_BANK_EMAIL))
    assert sorted(t["kind"] for t in transactions(db)) == ["expense", "income"]


def test_a_debit_matching_two_credits_goes_to_review(db: Database, user_id: str, acct: dict[str, str]) -> None:
    ingest(db, user_id, (CREDIT, OTHER_BANK_EMAIL), (CREDIT | {"date": "2026-09-02"}, OTHER_BANK_EMAIL))
    ingest(db, user_id, (DEBIT, BANK_EMAIL))
    debit: dict[str, Any] = next(t for t in transactions(db) if t["from"] == acct["bank"])
    assert (debit["kind"], debit["status"]) == ("expense", "needs_review")
    assert "possible transfer: 2 matching transactions" in debit["reason"]


def test_the_same_reference_picks_one_of_two_candidates(db: Database, user_id: str, acct: dict[str, str]) -> None:
    ingest(db, user_id, (CREDIT | {"reference_number": "R9"}, OTHER_BANK_EMAIL),
           (CREDIT | {"date": "2026-09-02"}, OTHER_BANK_EMAIL))
    ingest(db, user_id, (DEBIT | {"reference_number": "R9"}, BANK_EMAIL))
    assert sorted(t["kind"] for t in transactions(db)) == ["income", "transfer"]


def test_a_partner_the_user_edited_is_not_absorbed(db: Database, user_id: str, acct: dict[str, str],
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """The later-anchored row of a pair is deleted when absorbed; one carrying the user's edits never is."""
    with monkeypatch.context() as m:
        m.setattr(derive, "_pair", lambda run, txn: None)
        ingest(db, user_id, (DEBIT | {"date": recent(2)}, BANK_EMAIL), (CREDIT | {"date": recent(1)}, OTHER_BANK_EMAIL))
    credit: dict[str, Any] = next(t for t in transactions(db) if t["kind"] == "income")
    with db.write_session() as s:
        txns.apply_edits(s, user_id, credit["id"], {"transaction_date": recent(0)})
    ingest(db, user_id, (contract(amount=100, account_identifier="3333"), CARD_EMAIL))  # the rolling re-exam runs
    rows: list[dict[str, Any]] = transactions(db)
    assert len(rows) == 3 and credit["id"] in {t["id"] for t in rows}
    debit: dict[str, Any] = next(t for t in rows if t["from"] == acct["bank"])
    assert (debit["kind"], debit["status"]) == ("expense", "needs_review")
    assert "possible transfer: matches a transaction you've edited" in debit["reason"]


def test_reparsing_a_merged_transfer_keeps_one_transfer(db: Database, user_id: str, acct: dict[str, str]) -> None:
    _, ids = ingest(db, user_id, (DEBIT, BANK_EMAIL), (CREDIT, OTHER_BANK_EMAIL))
    [before] = transactions(db)
    reingest(db, user_id, [(ids[0], (DEBIT, BANK_EMAIL)), (ids[1], (CREDIT, OTHER_BANK_EMAIL))])
    [after] = transactions(db)
    assert after == before


def test_a_counterparty_that_is_one_of_your_accounts_makes_a_transfer(db: Database, user_id: str,
                                                                      acct: dict[str, str]) -> None:
    """The bank side of a card bill: the debit names the card, and no credit ever arrives."""
    ingest(db, user_id, (DEBIT | {"counterparty_identifier": "XX3333", "payee_raw": "CARD PAYMENT"}, BANK_EMAIL))
    [txn] = transactions(db)
    assert (txn["kind"], txn["from"], txn["to"], txn["status"]) == ("transfer", acct["bank"], acct["card"],
                                                                    "confirmed")


def test_a_counterparty_that_is_the_same_account_is_ignored(db: Database, user_id: str, acct: dict[str, str]) -> None:
    ingest(db, user_id, (DEBIT | {"counterparty_identifier": "1111"}, BANK_EMAIL))
    assert [(t["kind"], t["to"]) for t in transactions(db)] == [("expense", None)]


def test_a_lone_card_payment_is_a_transfer_with_the_payer_unknown(db: Database, user_id: str,
                                                                  acct: dict[str, str]) -> None:
    ingest(db, user_id, (contract(direction="credit", account_identifier="3333", payee_raw=None), CARD_EMAIL))
    [txn] = transactions(db)
    assert (txn["kind"], txn["from"], txn["to"], txn["status"]) == ("transfer", None, acct["card"], "needs_review")
    assert txn["reason"] == "card payment received: which of your accounts paid it?"


def test_a_card_credit_naming_a_merchant_is_a_refund(db: Database, user_id: str, acct: dict[str, str]) -> None:
    ingest(db, user_id, (contract(direction="credit", account_identifier="3333"), CARD_EMAIL))
    assert [(t["kind"], t["to"]) for t in transactions(db)] == [("refund", acct["card"])]


def test_a_bank_debit_fills_the_payer_of_a_lone_card_payment(db: Database, user_id: str, acct: dict[str, str]) -> None:
    _, [card_side] = ingest(db, user_id, (contract(direction="credit", account_identifier="3333", payee_raw=None),
                                          CARD_EMAIL))
    _, [bank_side] = ingest(db, user_id, (DEBIT | {"payee_raw": "CRED", "date": "2026-08-30"}, BANK_EMAIL))
    [txn] = transactions(db)
    assert (txn["kind"], txn["from"], txn["to"], txn["status"]) == ("transfer", acct["bank"], acct["card"],
                                                                    "confirmed")
    assert txn["sources"] == sorted([card_side, bank_side])


def test_a_card_purchase_never_pairs_with_a_credit_into_a_bank(db: Database, user_id: str,
                                                              acct: dict[str, str]) -> None:
    """Money can't leave a card for one of your accounts: a card debit is a purchase. A friend repaying the
    same amount into the bank that day is income, not the other half of a transfer (user, 2026-10-19)."""
    ingest(db, user_id, (contract(account_identifier="3333", payee_raw="TRAVEL SITE"), CARD_EMAIL),
           (contract(direction="credit", account_identifier="1111", payee_raw="A FRIEND"), BANK_EMAIL))
    assert sorted((t["kind"], t["from"], t["to"]) for t in transactions(db)) == [
        ("expense", acct["card"], None), ("income", None, acct["bank"])]


def test_an_unresolved_account_never_pairs(db: Database, user_id: str, acct: dict[str, str]) -> None:
    ingest(db, user_id, (DEBIT | {"account_identifier": "9999"}, BANK_EMAIL), (CREDIT, OTHER_BANK_EMAIL))
    assert sorted(t["kind"] for t in transactions(db)) == ["expense", "income"]


# --- check 4: the two card repayment fixtures, read from disk at runtime -------------------------

def _expected(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((CARDS / f"{name}.expected.json").read_text())
    return loaded


@pytest.mark.skipif(not CARDS.is_dir(), reason="fixtures are local-only (gitignored)")
@pytest.mark.parametrize("name", ["icici_cc_repayment_01", "scapia_cc_repayment_01"])
def test_card_repayment_fixtures_are_transfers_in_neither_total(db: Database, user_id: str, name: str) -> None:
    identifier: str = _expected(name)["account_identifier"]
    with db.write_session() as s:
        sync_seeds(s)
        by_digits: bool = identifier.isdigit() and len(identifier) == 4
        card: str = accounts.create_account(s, user_id, type="credit_card", display_name="Card", opening_balance=0,
                                            opening_balance_date="2026-01-01",
                                            last4=identifier if by_digits else "4444").id
        accounts.create_account(s, user_id, type="bank", display_name="Savings", opening_balance=0,
                                opening_balance_date="2026-01-01", last4="0000")
        if not by_digits:  # Scapia's repayment email names the card, not its digits
            accounts.assign_identifier(s, user_id, card, identifier)
        source_id: str = add_source(s, user_id, (CARDS / f"{name}.eml").read_bytes(), name).id
    llm: Llm = Llm(AssertionError("a seeded format must not reach the LLM"))
    with db.read_session() as s:
        batch: list[tuple[str, Routed]] = [(source_id, route(s, s.get_one(Source, source_id), llm, MODEL))]
    with db.write_session() as s:
        apply(s, user_id, batch)
    with db.read_session() as s:
        txn: TransactionEffective = s.scalars(select(TransactionEffective)).one()
        assert (txn.kind, txn.from_account_id, txn.to_account_id) == ("transfer", None, card)
        assert txn.amount == _expected(name)["amount"]
        totals: dict[str, int] = dict(s.execute(select(TransactionEffective.kind, func.sum(TransactionEffective.amount))
                                                .group_by(TransactionEffective.kind)).tuples().all())
        assert "expense" not in totals and "income" not in totals  # in neither total
    assert llm.calls == 0
