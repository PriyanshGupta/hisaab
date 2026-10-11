"""Manual entry, the overlay (edit / re-edit / undo), and manual delete."""

import hashlib
import json

import pytest
from sqlalchemy import func, select

from hisaab.accounts import repository as accounts
from hisaab.db import Database
from hisaab.models import Source, Transaction, TransactionEdit, TransactionSource
from hisaab.transactions import repository as txns
from hisaab.transactions.legs import LegError
from hisaab.transactions.repository import EditsWouldBeLost, NotFound, TransactionError
from tests import factories as f

DAY = "2026-10-05"


@pytest.fixture
def db(migrated_engine):
    return Database(migrated_engine)


@pytest.fixture
def asha(conn):
    uid = f.user(conn)["id"]
    conn.commit()
    return uid


def make_account(db, user_id, type="bank", last4="5512", name=None):
    with db.write_session() as s:
        return accounts.create_account(
            s, user_id, type=type, display_name=name or f"{type} {last4}", last4=None if type == "cash" else last4,
            opening_balance=0, opening_balance_date="2026-07-01",
        ).id


@pytest.fixture
def wallet(db, asha):
    return make_account(db, asha, type="cash", name="Wallet")


@pytest.fixture
def bank(db, asha):
    return make_account(db, asha)


def enter(db, user_id, **kw):
    args = {"kind": "expense", "amount": 40000, "transaction_date": DAY, "payee_raw": "Tea stall"} | kw
    with db.write_session() as s:
        return txns.create_manual(s, user_id, **args).id


def effective(db, user_id, txn_id):
    with db.read_session() as s:
        t = txns.get_effective(s, user_id, txn_id)
        return {c: getattr(t, c) for c in ("amount", "kind", "transaction_date", "from_account_id", "to_account_id")}


def edit_row(db, txn_id, field):
    with db.read_session() as s:
        e = s.scalars(select(TransactionEdit).where(TransactionEdit.transaction_id == txn_id,
                                                    TransactionEdit.field == field)).one_or_none()
        return None if e is None else (e.prior_value, e.new_value)


def apply(db, user_id, txn_id, **changes):
    with db.write_session() as s:
        txns.apply_edits(s, user_id, txn_id, changes)


def revert(db, user_id, txn_id, *fields):
    with db.write_session() as s:
        txns.revert(s, user_id, txn_id, list(fields))


# --- manual entry -----------------------------------------------------------------

def test_manual_entry_creates_a_manual_source_and_a_confirmed_transaction(db, asha, wallet):
    txn_id = enter(db, asha, from_account_id=wallet)
    with db.read_session() as s:
        txn = s.get(Transaction, txn_id)
        source = s.get(Source, txn.primary_source_id)
        link = s.scalars(select(TransactionSource).where(TransactionSource.transaction_id == txn_id)).one()
        assert (source.kind, source.user_id, source.processing_state) == ("manual", asha, "parsed")
        assert link.source_id == source.id
        assert (txn.status, txn.amount, txn.kind, txn.from_account_id, txn.to_account_id, txn.payee_raw) == (
            "confirmed", 40000, "expense", wallet, None, "Tea stall")
        # The typed values are the Source, kept so the entry is self-describing.
        entered = json.loads(source.content)
        assert entered["amount"] == 40000 and entered["payee_raw"] == "Tea stall"
        assert source.content_hash == hashlib.sha256(source.content).hexdigest()
        assert txn.derived_key == hashlib.sha256(f"{asha}:{source.ingest_seq}:0".encode()).hexdigest()


def test_each_entry_gets_the_next_ingest_seq(db, asha, wallet):
    a, b = enter(db, asha, from_account_id=wallet), enter(db, asha, from_account_id=wallet)
    with db.read_session() as s:
        seqs = [s.get(Source, s.get(Transaction, t).primary_source_id).ingest_seq for t in (a, b)]
    assert seqs[1] == seqs[0] + 1


@pytest.mark.parametrize("kind,legs", [
    ("expense", {"from_account_id": "W"}),
    ("income", {"to_account_id": "B"}),
    ("transfer", {"from_account_id": "B", "to_account_id": "W"}),
    ("refund", {"to_account_id": "W"}),  # a cash refund (ruling 2026-10-11)
])
def test_manual_kinds(db, asha, wallet, bank, kind, legs):
    ids = {"W": wallet, "B": bank}
    enter(db, asha, kind=kind, **{k: ids[v] for k, v in legs.items()})


@pytest.mark.parametrize("kw,error", [
    ({"kind": "income", "from_account_id": "W"}, LegError),
    ({"kind": "adjustment", "to_account_id": "W"}, TransactionError),  # system-made, not typed
    ({"kind": "lending", "from_account_id": "W"}, TransactionError),
    ({"amount": 0}, TransactionError),
    ({"amount": -500}, TransactionError),
    ({"amount": 10**15 + 1}, TransactionError),
    ({"transaction_date": "2099-01-01"}, TransactionError),
    ({"transaction_date": "05/10/2026"}, TransactionError),
    ({"transaction_time": "25:00"}, TransactionError),
])
def test_manual_entry_validation(db, asha, wallet, kw, error):
    args = {"from_account_id": wallet} | {k: (wallet if v == "W" else v) for k, v in kw.items()}
    with pytest.raises(error):
        enter(db, asha, **args)


def test_an_archived_account_cannot_be_used(db, asha, wallet):
    with db.write_session() as s:
        accounts.archive_account(s, asha, wallet)
    with pytest.raises(TransactionError, match="archived"):
        enter(db, asha, from_account_id=wallet)


def test_another_users_account_cannot_be_used(db, conn, asha):
    ravi = f.user(conn)["id"]
    conn.commit()
    ravis_wallet = make_account(db, ravi, type="cash")
    with pytest.raises(TransactionError, match="No such account"):
        enter(db, asha, from_account_id=ravis_wallet)


# --- the overlay: the check that matters --------------------------------------------

def test_prior_value_always_holds_what_the_source_said(db, asha, wallet):
    txn = enter(db, asha, from_account_id=wallet, amount=40000)

    apply(db, asha, txn, amount=45000)
    assert edit_row(db, txn, "amount") == ("40000", "45000")
    assert effective(db, asha, txn)["amount"] == 45000

    apply(db, asha, txn, amount=50000)  # a second correction
    assert edit_row(db, txn, "amount") == ("40000", "50000")  # prior is still the SOURCE, not 45000

    revert(db, asha, txn, "amount")
    assert edit_row(db, txn, "amount") is None
    assert effective(db, asha, txn)["amount"] == 40000  # back to the truth, not to the first edit


def test_the_base_row_is_never_touched_by_edits(db, asha, wallet):
    txn = enter(db, asha, from_account_id=wallet, amount=40000)
    apply(db, asha, txn, amount=99900, transaction_date="2026-10-01")
    with db.read_session() as s:
        base = s.get(Transaction, txn)
        assert (base.amount, base.transaction_date) == (40000, DAY)


def test_editing_back_to_the_source_value_removes_the_edit(db, asha, wallet):
    txn = enter(db, asha, from_account_id=wallet, amount=40000)
    apply(db, asha, txn, amount=45000)
    apply(db, asha, txn, amount=40000)
    assert edit_row(db, txn, "amount") is None


def test_an_expense_becomes_a_transfer_in_one_change(db, asha, wallet, bank):
    txn = enter(db, asha, from_account_id=bank, payee_raw="ATM")
    apply(db, asha, txn, kind="transfer", to_account_id=wallet)
    assert effective(db, asha, txn) | {} == {"amount": 40000, "kind": "transfer", "transaction_date": DAY,
                                             "from_account_id": bank, "to_account_id": wallet}
    assert edit_row(db, txn, "kind") == ("expense", "transfer")
    assert edit_row(db, txn, "to_account_id") == (None, wallet)


def test_an_invalid_combination_is_refused_whole(db, asha, wallet):
    txn = enter(db, asha, from_account_id=wallet)
    with pytest.raises(LegError):
        apply(db, asha, txn, kind="transfer")  # no destination account
    assert edit_row(db, txn, "kind") is None


def test_undo_that_would_break_the_legs_is_refused(db, asha, wallet, bank):
    txn = enter(db, asha, from_account_id=bank)
    apply(db, asha, txn, kind="transfer", to_account_id=wallet)
    with pytest.raises(LegError):
        revert(db, asha, txn, "kind")  # an expense with a destination account is invalid
    assert effective(db, asha, txn)["kind"] == "transfer"
    revert(db, asha, txn, "kind", "to_account_id")  # undoing both together is fine
    assert effective(db, asha, txn)["kind"] == "expense"


@pytest.mark.parametrize("changes,message", [
    ({"amount": 0}, "more than"),
    ({"amount": "45000"}, "whole number"),
    ({"transaction_date": "2099-01-01"}, "future"),
    ({"kind": "lending"}, "not supported"),
    ({"payee_raw": "x"}, "can't be edited"),
    ({"status": "confirmed"}, "can't be edited"),
])
def test_edit_validation(db, asha, wallet, changes, message):
    txn = enter(db, asha, from_account_id=wallet)
    with pytest.raises((TransactionError, LegError), match=message):
        apply(db, asha, txn, **changes)


def test_editing_an_account_to_another_users_is_refused(db, conn, asha, wallet):
    ravi = f.user(conn)["id"]
    conn.commit()
    ravis_wallet = make_account(db, ravi, type="cash")
    txn = enter(db, asha, from_account_id=wallet)
    with pytest.raises(TransactionError, match="No such account"):
        apply(db, asha, txn, from_account_id=ravis_wallet)


def test_another_users_transaction_is_invisible(db, conn, asha, wallet):
    ravi = f.user(conn)["id"]
    conn.commit()
    txn = enter(db, asha, from_account_id=wallet)
    with db.read_session() as s:
        assert txns.list_transactions(s, ravi) == []
        with pytest.raises(NotFound):
            txns.get_effective(s, ravi, txn)
    for action in (lambda s: txns.apply_edits(s, ravi, txn, {"amount": 1}),
                   lambda s: txns.revert(s, ravi, txn, ["amount"]),
                   lambda s: txns.delete_manual(s, ravi, txn, confirm_edits_lost=True)):
        with pytest.raises(NotFound):
            with db.write_session() as s:
                action(s)


# --- list ------------------------------------------------------------------------

def test_list_shows_effective_values_newest_first_with_provenance_and_edits(db, asha, wallet):
    old = enter(db, asha, from_account_id=wallet, transaction_date="2026-10-01")
    new = enter(db, asha, from_account_id=wallet, transaction_date="2026-10-06")
    apply(db, asha, old, amount=12300)
    with db.read_session() as s:
        rows = txns.list_transactions(s, asha)
        assert [(r.transaction.id, r.transaction.amount, r.provenance) for r in rows] == [
            (new, 40000, "manual"), (old, 12300, "manual")]
        assert [(e.field, e.prior_value, e.new_value) for e in rows[1].edits] == [("amount", "40000", "12300")]


# --- delete ----------------------------------------------------------------------

def count(db, model):
    with db.read_session() as s:
        return s.scalar(select(func.count()).select_from(model))


def test_delete_removes_the_entry_and_its_source(db, asha, wallet):
    txn = enter(db, asha, from_account_id=wallet)
    with db.write_session() as s:
        txns.delete_manual(s, asha, txn)
    assert (count(db, Transaction), count(db, Source), count(db, TransactionSource)) == (0, 0, 0)


def test_delete_with_edits_needs_confirmation(db, asha, wallet):
    txn = enter(db, asha, from_account_id=wallet)
    apply(db, asha, txn, amount=45000)
    with pytest.raises(EditsWouldBeLost, match="amount"):
        with db.write_session() as s:
            txns.delete_manual(s, asha, txn)
    assert count(db, Transaction) == 1
    with db.write_session() as s:
        txns.delete_manual(s, asha, txn, confirm_edits_lost=True)
    assert (count(db, Transaction), count(db, TransactionEdit), count(db, Source)) == (0, 0, 0)


def test_only_manual_entries_can_be_deleted(db, conn, asha):
    parsed = f.transaction(conn, user_id=asha)  # primary source is an email
    conn.commit()
    with pytest.raises(TransactionError, match="Only manual entries"):
        with db.write_session() as s:
            txns.delete_manual(s, asha, parsed["id"])
