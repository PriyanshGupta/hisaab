"""Captured payee corrections and their promotion (PRD §5.1 Loop 1, §5.6; tech-design §6.4).

A correction is a user's payee edit on a transaction carrying raw payee text. The admin sees each one with the
raw text, the chosen name, how many transactions share that text and who reported it; promoting writes a global
pattern that applies to history in the database alone. Every payee and raw string here is invented.
"""

from typing import Any

import pytest
from sqlalchemy import Connection, select

from hisaab.db import Database
from hisaab.models import NormalizationPattern, Payee, Transaction, TransactionEffective
from hisaab.payees.corrections import Correction, InvalidPattern, PromoteResult, corrections, dismiss, promote
from tests import factories as f

RAW: str = "UPI-CORNERGROCER-OKBANK"


@pytest.fixture
def people(conn: Connection) -> dict[str, str]:
    out: dict[str, str] = {"asha": f.user(conn, display_name="Asha")["id"], "ravi": f.user(conn, display_name="Ravi")["id"],
                           "meera": f.user(conn, display_name="Meera")["id"]}
    conn.commit()
    return out


def row(conn: Connection, user_id: str, raw: str | None = RAW, payee_id: str | None = None, **kw: Any) -> str:
    txn_id: str = f.transaction(conn, user_id, payee_raw=raw, payee_id=payee_id, **kw)["id"]
    conn.commit()
    return txn_id


def correct(conn: Connection, user_id: str, txn_id: str, payee_id: str) -> None:
    f.transaction_edit(conn, txn_id, user_id, field="payee_id", new_value=payee_id)
    conn.commit()


def payee(conn: Connection, name: str) -> str:
    pid: str = f.payee(conn, normalized_name=name)["id"]
    conn.commit()
    return pid


def listed(db: Database) -> list[Correction]:
    with db.read_session() as s:
        return corrections(s)


def test_edits_on_one_raw_text_to_one_payee_are_one_row_with_every_reporter(
        db: Database, conn: Connection, people: dict[str, str]) -> None:
    grocer: str = payee(conn, "Corner Grocer")
    for who, n in (("asha", 2), ("ravi", 1)):
        for _ in range(n):
            correct(conn, people[who], row(conn, people[who]), grocer)
    row(conn, people["meera"], raw="upi cornergrocer okbank")  # the same text, punctuation and case aside
    row(conn, people["meera"], raw="SOMETHING ELSE")
    [c] = listed(db)
    assert (c.raw, c.payee_id, c.payee_name, c.shared) == (RAW, grocer, "Corner Grocer", 4)
    assert sorted(c.reporters) == [("Asha", 2), ("Ravi", 1)]


def test_two_names_for_one_raw_text_are_two_rows(db: Database, conn: Connection, people: dict[str, str]) -> None:
    correct(conn, people["asha"], row(conn, people["asha"]), payee(conn, "Corner Grocer"))
    correct(conn, people["ravi"], row(conn, people["ravi"]), payee(conn, "Corner Store"))
    assert sorted((c.payee_name, c.reporters[0][0]) for c in listed(db)) == [("Corner Grocer", "Asha"),
                                                                              ("Corner Store", "Ravi")]


def test_an_edit_on_a_row_without_raw_text_is_not_a_correction(db: Database, conn: Connection,
                                                               people: dict[str, str]) -> None:
    correct(conn, people["asha"], row(conn, people["asha"], raw=None), payee(conn, "Corner Grocer"))
    assert listed(db) == []


def test_a_row_goes_once_a_pattern_covers_it_or_it_is_dismissed(db: Database, conn: Connection,
                                                                 people: dict[str, str]) -> None:
    grocer, store = payee(conn, "Corner Grocer"), payee(conn, "Corner Store")
    correct(conn, people["asha"], row(conn, people["asha"]), grocer)
    correct(conn, people["ravi"], row(conn, people["ravi"]), store)
    f.normalization_pattern(conn, match_type="exact", pattern=RAW.lower(), payee_id=grocer, origin="admin")
    conn.commit()
    assert [c.payee_name for c in listed(db)] == ["Corner Store"]
    with db.write_session() as s:
        dismiss(s, listed(db)[0].key, store, people["asha"])
    assert listed(db) == []


def effective(db: Database, txn_id: str) -> TransactionEffective:
    with db.read_session() as s:
        t: TransactionEffective = s.scalars(select(TransactionEffective).where(TransactionEffective.id == txn_id)).one()
        s.expunge(t)
        return t


def test_promoting_writes_a_global_pattern_and_names_everyones_history(
        db: Database, conn: Connection, people: dict[str, str]) -> None:
    grocer: str = payee(conn, "Corner Grocer")
    other: str = payee(conn, "Someone Else")
    edited: str = row(conn, people["asha"])
    correct(conn, people["asha"], edited, grocer)
    theirs: str = row(conn, people["ravi"], status="needs_review", review_reason="payee not recognised")
    renamed: str = row(conn, people["meera"])
    correct(conn, people["meera"], renamed, other)  # her own answer stays hers
    with db.write_session() as s:
        result: PromoteResult = promote(s, payee_id=grocer, payee_name=None, match_type="exact", pattern=RAW)
    assert (result.named, result.users) == (3, 3)
    with db.read_session() as s:
        p: NormalizationPattern = s.get_one(NormalizationPattern, result.pattern_id)
        assert (p.match_type, p.pattern, p.payee_id, p.origin, p.priority) == ("exact", RAW, grocer, "admin", 100)
    assert effective(db, theirs).payee_id == grocer
    assert "payee not recognised" not in (effective(db, theirs).review_reason or "")  # that question is answered
    assert effective(db, renamed).payee_id == other
    assert listed(db) == [c for c in listed(db) if c.payee_id == other]  # the promoted one is covered now


def test_promoting_an_edited_contains_pattern_with_a_new_name(db: Database, conn: Connection,
                                                              people: dict[str, str]) -> None:
    correct(conn, people["asha"], row(conn, people["asha"]), payee(conn, "Corner Grocer"))
    wider: str = row(conn, people["ravi"], raw="POS CORNERGROCER BLR")
    with db.write_session() as s:
        result: PromoteResult = promote(s, payee_id=None, payee_name="Corner Grocery Store", match_type="contains",
                                        pattern="cornergrocer")
    assert result.named == 2
    with db.read_session() as s:
        name: str | None = s.scalar(select(Payee.normalized_name).join(
            TransactionEffective, TransactionEffective.payee_id == Payee.id).where(TransactionEffective.id == wider))
    assert name == "Corner Grocery Store"


def test_a_promoted_pattern_overrides_a_weaker_one_but_not_a_stronger_one(
        db: Database, conn: Connection, people: dict[str, str]) -> None:
    corner, grocer, pinned = payee(conn, "Corner"), payee(conn, "Corner Grocer"), payee(conn, "Pinned Shop")
    f.normalization_pattern(conn, match_type="contains", pattern="corner", payee_id=corner, origin="seed", priority=0)
    f.normalization_pattern(conn, match_type="exact", pattern="POS CORNER PINNED", payee_id=pinned, origin="admin",
                            priority=500)
    conn.commit()
    weak: str = row(conn, people["ravi"], payee_id=corner)
    strong: str = row(conn, people["ravi"], raw="POS CORNER PINNED", payee_id=pinned)
    with db.write_session() as s:
        promote(s, payee_id=grocer, payee_name=None, match_type="contains", pattern="corner")
    assert effective(db, weak).payee_id == grocer
    assert effective(db, strong).payee_id == pinned


@pytest.mark.parametrize(("match_type", "pattern"), [("regex", "(" + "a" * 600 + ")"), ("regex", "(?<=x)y"),
                                                     ("exact", "  --  "), ("contains", ""), ("fuzzy", "corner")])
def test_an_unsafe_or_empty_pattern_is_refused_and_writes_nothing(
        db: Database, conn: Connection, people: dict[str, str], match_type: str, pattern: str) -> None:
    grocer: str = payee(conn, "Corner Grocer")
    with db.write_session() as s, pytest.raises(InvalidPattern):
        promote(s, payee_id=grocer, payee_name=None, match_type=match_type, pattern=pattern)
    with db.read_session() as s:
        assert s.scalars(select(NormalizationPattern)).all() == []
        assert s.scalars(select(Transaction.payee_id).where(Transaction.payee_id.is_not(None))).all() == []


def test_promote_never_touches_a_manual_entry_and_manual_rows_are_not_corrections(
        db: Database, conn: Connection, people: dict[str, str]) -> None:
    """Slice 24 review: a typed entry keeps the payee the user typed (its payee_raw is that typed name)."""
    express, mart = payee(conn, "Lantern Mart Express"), payee(conn, "Lantern Mart")
    typed_src: str = f.source(conn, people["asha"], kind="manual")["id"]
    typed: str = f.transaction(conn, people["asha"], source_id=typed_src, payee_raw="Lantern Mart Express",
                               payee_id=express)["id"]
    manual_edit: str = f.transaction(conn, people["asha"], source_id=f.source(conn, people["asha"], kind="manual")["id"],
                                     payee_raw="LANTERNMART ORDER")["id"]
    correct(conn, people["asha"], manual_edit, mart)
    emailed: str = row(conn, people["ravi"], raw="LANTERNMART ORDER 42")
    assert listed(db) == []  # an edit on a typed entry isn't a correction of what an alert printed
    with db.write_session() as s:
        result: PromoteResult = promote(s, payee_id=mart, payee_name=None, match_type="contains", pattern="lanternmart")
    assert result.named == 1
    assert effective(db, typed).payee_id == express and effective(db, emailed).payee_id == mart
