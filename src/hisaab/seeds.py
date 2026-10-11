"""Seed data shipped with the code (slice 11): parser templates and normalization patterns.

    python -m hisaab sync-seeds        (scripts/run.sh runs it after migrations)

- Templates: every JSON spec in parsing/seed/ is a hand-authored template named
  after its file (hdfc_upi_debit.json -> "hdfc_upi_debit"). A new file is added
  as active; an edited one updates the stored spec. Status is left alone, so a
  template the demotion loop retired (slice 16) stays retired until someone
  reactivates it. Templates the LLM proposed are never touched.
- Normalization: payees/seed_patterns.json lists public merchant brands seen in
  the fixture corpus (PRD §5.6: Loop 1 ships pre-seeded) and the patterns that
  map raw strings to them. People and their UPI IDs are never seeded: the
  fixtures are real and must not reach the repository.

Idempotent: running it twice changes nothing. One transaction: an invalid seed
stops the whole sync with the file named, and nothing is half-loaded.
"""

import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hisaab.models import NormalizationPattern, ParserTemplate, Payee, Sender
from hisaab.parsing.email_spec import InvalidSpec, parse_email_spec
from hisaab.parsing.statement_spec import InvalidStatementSpec, parse_statement_spec
from hisaab.parsing.templates import save_email_template, save_statement_template
from hisaab.payees import repository as payees

SEED_TEMPLATES: Path = Path(str(files("hisaab.parsing") / "seed"))
SEED_PATTERNS: Path = Path(str(files("hisaab.payees") / "seed_patterns.json"))


@dataclass(frozen=True)
class SeedReport:
    templates_added: int
    templates_updated: int
    payees_added: int
    patterns_added: int
    patterns_updated: int = 0
    senders_added: int = 0


def sync_seeds(session: Session, template_dir: Path = SEED_TEMPLATES, patterns_file: Path = SEED_PATTERNS) -> SeedReport:
    added, updated = _sync_templates(session, template_dir)
    payees_added, patterns_added, patterns_updated = _sync_patterns(session, patterns_file)
    senders_added: int = _sync_senders(session, template_dir)
    return SeedReport(added, updated, payees_added, patterns_added, patterns_updated, senders_added)


def _sync_senders(session: Session, template_dir: Path) -> int:
    """Every sender a seed email spec lists joins the registry as active (PRD §4.3 track 1).
    An existing sender keeps its status: one the admin ignored stays ignored."""
    known: set[str] = set(session.scalars(select(Sender.email_address)))
    added: int = 0
    for path in sorted(template_dir.glob("*.json")):
        spec: dict[str, Any] = json.loads(path.read_text())
        if spec.get("artifact_type") == "statement":
            continue
        for address in parse_email_spec(spec).senders:  # specs were validated by _sync_templates
            if address not in known:
                session.add(Sender(email_address=address, status="active"))
                known.add(address)
                added += 1
    session.flush()
    return added


def _sync_templates(session: Session, template_dir: Path) -> tuple[int, int]:
    added: int = 0
    updated: int = 0
    for path in sorted(template_dir.glob("*.json")):
        spec: dict[str, Any] = json.loads(path.read_text())
        is_statement: bool = spec.get("artifact_type") == "statement"
        try:
            parsed_sender: str | None = None if is_statement else parse_email_spec(spec).senders[0]
            if is_statement:
                parse_statement_spec(spec)
        except (InvalidSpec, InvalidStatementSpec) as e:
            raise ValueError(f"seed template {path.name} is invalid: {e}") from None
        name: str = path.stem
        row: ParserTemplate | None = session.scalars(
            select(ParserTemplate).where(ParserTemplate.name == name, ParserTemplate.origin == "human")
        ).first()
        if row is None:
            if is_statement:
                save_statement_template(session, name=name, spec=spec, status="active")
            else:
                save_email_template(session, name=name, spec=spec, status="active")
            added += 1
        elif json.loads(row.spec) != spec:
            row.spec = json.dumps(spec, ensure_ascii=False)
            if not is_statement:
                row.sender_address, row.subject_pattern = parsed_sender, spec["match"]["subject"]
            updated += 1
    session.flush()
    return added, updated


def _sync_patterns(session: Session, patterns_file: Path) -> tuple[int, int, int]:
    payees_added: int = 0
    added: int = 0
    updated: int = 0
    for entry in json.loads(patterns_file.read_text()):
        payee: Payee | None = payees.find_by_name(session, entry["payee"])
        if payee is None:
            payee = payees.create_payee(session, entry["payee"], entry.get("type", "merchant"))
            payees_added += 1
        for p in entry["patterns"]:
            row: NormalizationPattern | None = session.scalars(select(NormalizationPattern).where(
                NormalizationPattern.match_type == p["match_type"], NormalizationPattern.pattern == p["pattern"],
                NormalizationPattern.origin == "seed")).first()
            if row is None:
                session.add(NormalizationPattern(match_type=p["match_type"], pattern=p["pattern"], payee_id=payee.id,
                                                 origin="seed", priority=p.get("priority", 0)))
                added += 1
            elif (row.payee_id, row.priority) != (payee.id, p.get("priority", 0)):
                row.payee_id, row.priority = payee.id, p.get("priority", 0)
                updated += 1
    session.flush()
    return payees_added, added, updated
