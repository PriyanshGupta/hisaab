"""Parsing and ingestion tables, transcribed from tech-design §3.3 (amended 2026-10-10).

Global tables (no user_id; shared by every user by design, PRD §5.2):
parser_templates, extraction_cache, format_sightings, normalization_patterns, senders.
"""

from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from hisaab.models.base import Base, created_column, id_column, one_of

ARTIFACT_TYPES: tuple[str, ...] = ("email", "statement")
TEMPLATE_ORIGINS: tuple[str, ...] = ("human", "llm_proposed")
TEMPLATE_STATUSES: tuple[str, ...] = ("proposed", "validated", "active", "demoted")
EXTRACTION_OUTCOMES: tuple[str, ...] = ("transaction", "non_transactional", "unparseable")
SIGHTING_STATES: tuple[str, ...] = ("accumulating", "proposed", "promoted", "excluded_non_transactional")
MATCH_TYPES: tuple[str, ...] = ("exact", "contains", "regex")
PATTERN_ORIGINS: tuple[str, ...] = ("seed", "admin")
SENDER_STATUSES: tuple[str, ...] = ("active", "candidate", "ignored")
GMAIL_STATUSES: tuple[str, ...] = ("connected", "needs_reauth", "disconnected")


class ParserTemplate(Base):  # GLOBAL
    __tablename__: str = "parser_templates"
    __table_args__: tuple[Any, ...] = (
        one_of("artifact_type", ARTIFACT_TYPES, "artifact_type"),
        one_of("origin", TEMPLATE_ORIGINS, "origin"),
        one_of("status", TEMPLATE_STATUSES, "status"),
    )

    id: Mapped[str] = id_column()
    artifact_type: Mapped[str]
    name: Mapped[str]
    sender_address: Mapped[str | None]  # email only
    subject_pattern: Mapped[str | None]  # email only
    spec: Mapped[str]  # JSON, read by the matching interpreter
    origin: Mapped[str]
    status: Mapped[str]
    priority: Mapped[int] = mapped_column(server_default="0")
    example_source_ids: Mapped[str | None]  # JSON array
    validation_result: Mapped[str | None]  # JSON, field-by-field replay
    consecutive_failures: Mapped[int] = mapped_column(server_default="0")
    failure_window_started_at: Mapped[str | None]
    created_at: Mapped[str] = created_column()
    promoted_at: Mapped[str | None]
    demoted_at: Mapped[str | None]


class ExtractionCache(Base):  # GLOBAL
    __tablename__: str = "extraction_cache"
    __table_args__: tuple[Any, ...] = (one_of("outcome", EXTRACTION_OUTCOMES, "outcome"),)

    content_hash: Mapped[str] = mapped_column(primary_key=True)
    outcome: Mapped[str]
    extracted: Mapped[str | None]  # JSON, the PRD §10.4 contract
    model: Mapped[str]
    tier: Mapped[int]
    created_at: Mapped[str] = created_column()


class FormatSighting(Base):  # GLOBAL
    __tablename__: str = "format_sightings"
    __table_args__: tuple[Any, ...] = (one_of("state", SIGHTING_STATES, "state"),)

    id: Mapped[str] = id_column()
    fingerprint: Mapped[str] = mapped_column(unique=True)
    sender_address: Mapped[str]
    subject_masked: Mapped[str]
    structural_markers: Mapped[str]  # JSON
    sighting_count: Mapped[int] = mapped_column(server_default="1")
    example_source_ids: Mapped[str]  # JSON array, capped at 3
    state: Mapped[str]
    template_id: Mapped[str | None] = mapped_column(ForeignKey("parser_templates.id"))
    first_seen_at: Mapped[str]
    last_seen_at: Mapped[str]


class NormalizationPattern(Base):  # GLOBAL
    __tablename__: str = "normalization_patterns"
    __table_args__: tuple[Any, ...] = (
        one_of("match_type", MATCH_TYPES, "match_type"),
        one_of("origin", PATTERN_ORIGINS, "origin"),
    )

    id: Mapped[str] = id_column()
    match_type: Mapped[str]
    pattern: Mapped[str]
    payee_id: Mapped[str] = mapped_column(ForeignKey("payees.id"))
    origin: Mapped[str]
    priority: Mapped[int] = mapped_column(server_default="0")
    created_at: Mapped[str] = created_column()


Index("ix_normpat_priority", NormalizationPattern.priority.desc(), NormalizationPattern.id)


class Sender(Base):  # GLOBAL
    __tablename__: str = "senders"
    __table_args__: tuple[Any, ...] = (one_of("status", SENDER_STATUSES, "status"),)

    id: Mapped[str] = id_column()
    email_address: Mapped[str] = mapped_column(unique=True)
    display_name: Mapped[str | None]
    status: Mapped[str]
    created_at: Mapped[str] = created_column()
    reviewed_at: Mapped[str | None]


class SenderCandidate(Base):
    __tablename__: str = "sender_candidates"
    __table_args__: tuple[Any, ...] = (UniqueConstraint("discovered_by_user_id", "sender_address"),)

    id: Mapped[str] = id_column()
    discovered_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    sender_address: Mapped[str]
    matched_subjects: Mapped[str]  # JSON: subjects + snippets ONLY, never bodies
    match_count: Mapped[int]
    first_seen_at: Mapped[str]
    discarded_at: Mapped[str | None]


class GmailConnection(Base):
    __tablename__: str = "gmail_connections"
    __table_args__: tuple[Any, ...] = (one_of("status", GMAIL_STATUSES, "status"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    email_address: Mapped[str]
    refresh_token_enc: Mapped[bytes]
    status: Mapped[str]
    last_successful_sync_at: Mapped[str | None]
    connected_at: Mapped[str]
    updated_at: Mapped[str]


class StatementStaging(Base):
    __tablename__: str = "statement_staging"

    id: Mapped[str] = id_column()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    # Nullable (ruling 2026-10-10): when header binding fails, the statement is
    # staged unbound and the preview asks the user to pick the account (§4.7).
    # Merge must refuse a row whose account_id is still NULL.
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))
    filename: Mapped[str]
    content_hash: Mapped[str]
    template_id: Mapped[str | None] = mapped_column(ForeignKey("parser_templates.id"))
    parsed_rows: Mapped[str]  # JSON
    row_dispositions: Mapped[str]  # JSON: new | matches:<txn_id> | unparseable
    declared_opening: Mapped[int | None] = mapped_column(BigInteger)
    declared_closing: Mapped[int | None] = mapped_column(BigInteger)
    validation_result: Mapped[str]  # JSON diagnostic (§4.8)
    created_at: Mapped[str] = created_column()
    expires_at: Mapped[str]


class CorrectionDismissal(Base):  # GLOBAL: the admin set this captured correction aside (slice 24, migration 0012)
    __tablename__: str = "correction_dismissals"
    __table_args__: tuple[Any, ...] = (UniqueConstraint("raw_key", "payee_id"),)

    id: Mapped[str] = id_column()
    raw_key: Mapped[str]  # the raw payee text squashed (lower-case letters and digits), as exact patterns match
    payee_id: Mapped[str] = mapped_column(ForeignKey("payees.id"))
    dismissed_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[str] = created_column()


RESYNC_REASONS: tuple[str, ...] = ("edited", "approved", "shipped_updated")


class ResyncNotice(Base):  # GLOBAL: a parser changed; history it read may now read differently (tech-design §5.2)
    __tablename__: str = "resync_notices"
    __table_args__: tuple[Any, ...] = (one_of("reason", RESYNC_REASONS, "reason"),)

    id: Mapped[str] = id_column()
    template_id: Mapped[str | None] = mapped_column(ForeignKey("parser_templates.id"))
    template_name: Mapped[str]
    reason: Mapped[str]
    range_from: Mapped[str]  # India date, YYYY-MM-DD: the first email that template read
    range_to: Mapped[str]  # the last one
    created_at: Mapped[str] = created_column()
    resolved_at: Mapped[str | None]
