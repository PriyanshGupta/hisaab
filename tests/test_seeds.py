"""Seed templates and normalization patterns: loaded idempotently by sync_seeds (slice 11)."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select

from hisaab.db import Database
from hisaab.models import NormalizationPattern, ParserTemplate, Payee
from hisaab.parsing.email_spec import parse_email_spec
from hisaab.parsing.statement_spec import parse_statement_spec
from hisaab.payees.normalization import PatternRule, match_payee
from hisaab.seeds import SEED_PATTERNS, SEED_TEMPLATES, SeedReport, sync_seeds
from tests.test_email_spec import SPEC
from tests.test_statement_spec import RULED

PATTERNS: list[dict[str, Any]] = [
    {"payee": "Swiggy", "type": "merchant", "patterns": [{"match_type": "contains", "pattern": "swiggy", "priority": 0}]},
    {"payee": "Swiggy Instamart", "type": "merchant",
     "patterns": [{"match_type": "contains", "pattern": "swiggy instamart", "priority": 10}]},
]


@pytest.fixture
def db(migrated_engine: Engine) -> Database:
    return Database(migrated_engine)


@pytest.fixture
def seed_dir(tmp_path: Path) -> Path:
    (tmp_path / "example_debit.json").write_text(json.dumps(SPEC))
    (tmp_path / "example_savings.json").write_text(json.dumps(RULED))
    (tmp_path / "patterns").mkdir()
    (tmp_path / "patterns" / "patterns.json").write_text(json.dumps(PATTERNS))
    return tmp_path


def sync(db: Database, seed_dir: Path) -> SeedReport:
    with db.write_session() as s:
        return sync_seeds(s, seed_dir, seed_dir / "patterns" / "patterns.json")


def rows(db: Database) -> list[tuple[str, str, str, str]]:
    with db.read_session() as s:
        return [(t.name, t.artifact_type, t.status, t.origin) for t in s.scalars(select(ParserTemplate).order_by(ParserTemplate.name))]


def test_first_sync_loads_every_template_active_and_every_pattern(db: Database, seed_dir: Path) -> None:
    report: SeedReport = sync(db, seed_dir)
    assert report == SeedReport(templates_added=2, templates_updated=0, payees_added=2, patterns_added=2)
    assert rows(db) == [("example_debit", "email", "active", "human"), ("example_savings", "statement", "active", "human")]
    with db.read_session() as s:
        found: list[tuple[str, str, int]] = [
            (p.pattern, s.get_one(Payee, p.payee_id).normalized_name, p.priority)
            for p in s.scalars(select(NormalizationPattern).order_by(NormalizationPattern.priority))]
        assert found == [("swiggy", "Swiggy", 0), ("swiggy instamart", "Swiggy Instamart", 10)]
        assert {p.origin for p in s.scalars(select(NormalizationPattern))} == {"seed"}


def test_a_second_sync_changes_nothing(db: Database, seed_dir: Path) -> None:
    sync(db, seed_dir)
    assert sync(db, seed_dir) == SeedReport(0, 0, 0, 0)
    with db.read_session() as s:
        assert s.execute(select(func.count()).select_from(ParserTemplate)).scalar_one() == 2


def test_an_edited_seed_spec_updates_the_stored_template_but_keeps_its_status(db: Database, seed_dir: Path) -> None:
    sync(db, seed_dir)
    with db.write_session() as s:
        s.scalars(select(ParserTemplate).where(ParserTemplate.name == "example_debit")).one().status = "demoted"
    edited: dict[str, Any] = copy.deepcopy(SPEC)
    edited["match"]["subject"] = "^Debit alert v2"
    (seed_dir / "example_debit.json").write_text(json.dumps(edited))
    assert sync(db, seed_dir).templates_updated == 1
    with db.read_session() as s:
        row: ParserTemplate = s.scalars(select(ParserTemplate).where(ParserTemplate.name == "example_debit")).one()
        assert (row.subject_pattern, row.status, json.loads(row.spec)) == ("^Debit alert v2", "demoted", edited)


def test_an_invalid_seed_spec_stops_the_sync_and_names_the_file(db: Database, seed_dir: Path) -> None:
    (seed_dir / "broken.json").write_text(json.dumps({**SPEC, "version": 7}))
    with pytest.raises(ValueError, match="broken.json"):
        sync(db, seed_dir)
    assert rows(db) == []  # nothing half-loaded: the caller's transaction rolls back


def test_a_template_not_authored_by_hand_is_never_touched(db: Database, seed_dir: Path) -> None:
    with db.write_session() as s:
        s.add(ParserTemplate(artifact_type="email", name="example_debit", sender_address="x@y.in", subject_pattern="x",
                             spec="{}", origin="llm_proposed", status="proposed"))
    sync(db, seed_dir)
    assert ("example_debit", "email", "proposed", "llm_proposed") in rows(db)
    assert ("example_debit", "email", "active", "human") in rows(db)


# --- the committed seeds themselves ---------------------------------------------------------

def test_every_committed_seed_template_is_valid() -> None:
    files: list[Path] = sorted(SEED_TEMPLATES.glob("*.json"))
    assert len(files) >= 14  # 9 email formats, 5 statement layouts
    for path in files:
        spec: dict[str, Any] = json.loads(path.read_text())
        (parse_statement_spec if spec.get("artifact_type") == "statement" else parse_email_spec)(spec)


def test_the_committed_seeds_load_into_a_fresh_database(db: Database) -> None:
    with db.write_session() as s:
        report: SeedReport = sync_seeds(s)
    assert report.templates_added == len(list(SEED_TEMPLATES.glob("*.json")))
    assert report.patterns_added > 0 and SEED_PATTERNS.is_file()


def _seed_rules(reverse_ids: bool = False) -> list[PatternRule]:
    """The committed patterns. Stored ids are random UUIDs, so tests try both id orders: only an
    explicit priority may decide between overlapping patterns."""
    entries: list[dict[str, Any]] = json.loads(SEED_PATTERNS.read_text())
    n: int = len(entries)
    return [PatternRule(f"{(n - i) if reverse_ids else i:03d}-{j}", p["match_type"], p["pattern"], e["payee"],
                        p.get("priority", 0))
            for i, e in enumerate(entries) for j, p in enumerate(e["patterns"])]


@pytest.mark.parametrize(
    ("raw", "payee"),
    [
        ("GOOGLE PLAY CONTENT PUMUMBAI", "Google Play"),
        ("UPI-GOOGLEINDIADIGITAL-GPAY-RECHARGE", "Google"),
        ("BUNDL TECHNOLOGIESBENGALURU", "Swiggy"),  # Swiggy's legal entity
        ("RAZ*SWIGGY BANGALORE", "Swiggy"),
        ("BLINK COMMERCE PVT LTDBANGALORE", "Blinkit"),
        ("BIGTREE ENTERTAINMENT Mumbai IN", "BookMyShow"),
        ("ZOMATO LIMITEDNEW DELHI", "Zomato"),
        ("BestBuySanfordUs", "Best Buy"),
        ("UPI-SOMEONE-PERSONAL-NAME", None),  # people are never seeded
    ],
)
def test_the_committed_patterns_resolve_brands_and_their_legal_names(raw: str, payee: str | None) -> None:
    assert match_payee(_seed_rules(), raw) == payee
    assert match_payee(_seed_rules(reverse_ids=True), raw) == payee


def test_every_committed_pattern_is_usable() -> None:
    for rule in _seed_rules():
        assert rule.match_type in ("exact", "contains", "regex") and rule.pattern.strip()
