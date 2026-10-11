"""Core entities, transcribed from tech-design §3.2 (amended 2026-10-10).

Key invariants that live in the schema itself:
- transactions.amount is always positive; direction is carried by the legs.
- A transfer has two distinct resolved legs.
- A leg is either resolved (an account id), unresolved (the raw identifier the
  parser saw, so review can ask about it), or external (both NULL). Never both.
- Nothing on `transactions` is user-authored. User edits live in
  transaction_edits and are overlaid by the transactions_effective view.

Invariants SQLite cannot express (leg validity per kind, cross-user
integrity) are enforced in the repository layer and by tests (§3.9).
"""

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from hisaab.models.base import Base, created_column, id_column, one_of, partial_index_where, updated_column

USER_ROLES = ("admin", "member")
ACCOUNT_TYPES = ("bank", "credit_card", "cash")
IDENTIFIER_KINDS = ("last4", "card_last4", "vpa", "other")
IDENTIFIER_ORIGINS = ("account_setup", "review_assignment")
SOURCE_KINDS = ("email", "statement", "manual")
PROCESSING_STATES = ("pending", "parsed", "pending_llm", "failed", "non_transactional")
TRANSACTION_KINDS = ("expense", "income", "transfer", "refund", "adjustment", "lending")
TRANSACTION_STATUSES = ("confirmed", "needs_review", "reversed")
PAYEE_TYPES = ("merchant", "person")
OBSERVATION_SOURCES = ("email", "statement", "manual")
DELTA_STATUSES = ("open", "accepted", "investigated")

# Edit fields whose effective value can never be NULL (see TransactionEdit).
NON_NULLABLE_EDIT_FIELDS = ("amount", "kind", "transaction_date", "review_acknowledged")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (one_of("role", USER_ROLES, "role"),)

    id: Mapped[str] = id_column()
    username: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]
    display_name: Mapped[str]
    role: Mapped[str]
    created_at: Mapped[str] = created_column()


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        # One ACTIVE account per last4 per user (ruling 2026-10-11, migration 0002):
        # archiving frees the last4 so a replacement account can use it. NULL last4
        # (cash accounts) never collides: NULLs are distinct in a unique index.
        Index("uq_accounts_active_last4", "user_id", "last4", unique=True,
              **partial_index_where("archived_at IS NULL")),
        one_of("type", ACCOUNT_TYPES, "type"),
    )

    id: Mapped[str] = id_column()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    type: Mapped[str]
    display_name: Mapped[str]
    institution: Mapped[str | None]
    account_number_enc: Mapped[bytes | None]  # encrypted at rest; NULL for cash
    last4: Mapped[str | None]  # derived; identifier matching happens on this
    opening_balance: Mapped[int] = mapped_column(BigInteger)  # paise; for cards POSITIVE = debt
    opening_balance_date: Mapped[str]
    archived_at: Mapped[str | None]
    created_at: Mapped[str] = created_column()


class AccountIdentifier(Base):
    __tablename__ = "account_identifiers"
    __table_args__ = (
        # Unique among ACTIVE identifiers only: archiving an account retires its
        # identifiers (retired_at), which then never resolve and may be reused,
        # e.g. a UPI ID re-linked to a new bank account (migration 0002).
        Index("uq_account_identifiers_active", "user_id", "kind", "identifier", unique=True,
              **partial_index_where("retired_at IS NULL")),
        one_of("kind", IDENTIFIER_KINDS, "kind"),
        one_of("origin", IDENTIFIER_ORIGINS, "origin"),
        Index("ix_acctid_lookup", "user_id", "identifier"),
    )

    id: Mapped[str] = id_column()
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str]
    identifier: Mapped[str]  # stored normalized
    origin: Mapped[str]
    created_at: Mapped[str] = created_column()
    retired_at: Mapped[str | None]  # set when its account is archived; retired identifiers never resolve


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        # NULL external_message_id (manual, statement) never collides.
        UniqueConstraint("user_id", "external_message_id"),
        one_of("kind", SOURCE_KINDS, "kind"),
        one_of("processing_state", PROCESSING_STATES, "processing_state"),
        Index("ix_sources_state", "processing_state", **partial_index_where("processing_state IN ('pending_llm', 'failed')")),
        Index("ix_sources_seq", "ingest_seq"),
    )

    id: Mapped[str] = id_column()
    # Monotonic, assigned by the single writer; anchors derived_key (§3.6).
    ingest_seq: Mapped[int] = mapped_column(unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str]
    content: Mapped[bytes | None]  # NULL = not retained (§3.7)
    content_hash: Mapped[str]  # extraction cache key; deliberately NOT unique (§11.2)
    external_message_id: Mapped[str | None]  # Gmail message id
    received_at: Mapped[str]  # UTC
    ingested_at: Mapped[str]
    processing_state: Mapped[str]
    diagnostic: Mapped[str | None]  # JSON; retained after content discard
    created_at: Mapped[str] = created_column()


class Payee(Base):  # GLOBAL
    __tablename__ = "payees"
    __table_args__ = (one_of("type", PAYEE_TYPES, "type"),)

    id: Mapped[str] = id_column()
    normalized_name: Mapped[str] = mapped_column(unique=True)
    type: Mapped[str]
    created_at: Mapped[str] = created_column()


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[str] = id_column()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str]
    archived_at: Mapped[str | None]
    created_at: Mapped[str] = created_column()


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("user_id", "derived_key"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(
            "kind <> 'transfer' OR (from_account_id IS NOT NULL AND to_account_id IS NOT NULL"
            " AND from_account_id <> to_account_id)",
            name="transfer_has_two_distinct_legs",
        ),
        CheckConstraint(
            "from_account_id IS NULL OR unresolved_from_identifier IS NULL",
            name="from_leg_not_both_resolved_and_unresolved",
        ),
        CheckConstraint(
            "to_account_id IS NULL OR unresolved_to_identifier IS NULL",
            name="to_leg_not_both_resolved_and_unresolved",
        ),
        one_of("kind", TRANSACTION_KINDS, "kind"),
        one_of("status", TRANSACTION_STATUSES, "status"),
        # Reporting
        Index("ix_txn_user_date", "user_id", "transaction_date"),
        Index("ix_txn_user_status", "user_id", "status", **partial_index_where("status = 'needs_review'")),
        Index("ix_txn_user_payee", "user_id", "payee_id"),
        Index("ix_txn_from_acct", "from_account_id"),
        Index("ix_txn_to_acct", "to_account_id"),
        # Deduplication
        Index("ix_txn_user_ref", "user_id", "reference_number", **partial_index_where("reference_number IS NOT NULL")),
        Index("ix_txn_dedup_fb", "user_id", "amount", "transaction_date"),
    )

    id: Mapped[str] = id_column()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    derived_key: Mapped[str]  # hash(user_id, anchor_ingest_seq, discriminator), §3.6
    amount: Mapped[int] = mapped_column(BigInteger)  # paise, always positive
    kind: Mapped[str]
    from_account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))
    to_account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))
    unresolved_from_identifier: Mapped[str | None]
    unresolved_to_identifier: Mapped[str | None]
    payee_id: Mapped[str | None] = mapped_column(ForeignKey("payees.id"))
    payee_raw: Mapped[str | None]  # raw string; enables retroactive normalization
    reference_number: Mapped[str | None]
    transaction_date: Mapped[str]
    transaction_time: Mapped[str | None]
    posted_date: Mapped[str | None]
    status: Mapped[str]
    review_reason: Mapped[str | None]
    primary_source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    reverses_transaction_id: Mapped[str | None] = mapped_column(ForeignKey("transactions.id"))
    refunds_transaction_id: Mapped[str | None] = mapped_column(ForeignKey("transactions.id"))
    created_at: Mapped[str] = created_column()
    updated_at: Mapped[str] = updated_column()


class TransactionSource(Base):
    __tablename__ = "transaction_sources"
    __table_args__ = (Index("ix_tsrc_source", "source_id"),)

    # Link rows only, so they may cascade with their transaction.
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id", ondelete="CASCADE"), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)


class TransactionEdit(Base):
    """One user override of one field: the overlay, physically (§3.5).

    The FK to transactions deliberately has NO cascade (ruling 2026-10-10):
    PRD §4.1 says a user's edit is never silently discarded. Deleting a
    transaction that still has edits fails, so whoever deletes it must decide
    what happens to them (route to review, or warn-then-delete per §10.6).
    """

    __tablename__ = "transaction_edits"
    __table_args__ = (
        UniqueConstraint("transaction_id", "field"),
        # new_value NULL means "edited to null", which is meaningless for these.
        CheckConstraint(
            "field NOT IN ({}) OR new_value IS NOT NULL".format(", ".join(f"'{f}'" for f in NON_NULLABLE_EDIT_FIELDS)),
            name="required_field_not_null",
        ),
        Index("ix_edits_txn", "transaction_id"),
    )

    id: Mapped[str] = id_column()
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    field: Mapped[str]  # whitelisted in application code
    prior_value: Mapped[str | None]  # the PARSER's value; never overwritten on re-edit
    new_value: Mapped[str | None]  # canonical string; NULL = edited to null
    created_at: Mapped[str] = created_column()
    updated_at: Mapped[str] = updated_column()


class PayeeCategory(Base):
    __tablename__ = "payee_categories"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    payee_id: Mapped[str] = mapped_column(ForeignKey("payees.id"), primary_key=True)
    category_id: Mapped[str] = mapped_column(ForeignKey("categories.id"))
    created_at: Mapped[str] = created_column()
    updated_at: Mapped[str] = updated_column()


class BalanceObservation(Base):
    __tablename__ = "balance_observations"
    __table_args__ = (one_of("source", OBSERVATION_SOURCES, "source"),)

    id: Mapped[str] = id_column()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    balance: Mapped[int] = mapped_column(BigInteger)
    observed_at: Mapped[str]
    source: Mapped[str]
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id"))
    created_at: Mapped[str] = created_column()


class BalanceDelta(Base):
    __tablename__ = "balance_deltas"
    __table_args__ = (
        one_of("status", DELTA_STATUSES, "status"),
        Index("ix_deltas_open", "user_id", "status", **partial_index_where("status = 'open'")),
    )

    id: Mapped[str] = id_column()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    computed_balance: Mapped[int] = mapped_column(BigInteger)
    observed_balance: Mapped[int] = mapped_column(BigInteger)
    delta: Mapped[int] = mapped_column(BigInteger)
    arose_from: Mapped[str]  # date the prior observation anchored
    observation_id: Mapped[str] = mapped_column(ForeignKey("balance_observations.id"))
    status: Mapped[str]
    adjustment_transaction_id: Mapped[str | None] = mapped_column(ForeignKey("transactions.id"))
    created_at: Mapped[str] = created_column()
    resolved_at: Mapped[str | None]


# Descending index; needs the column object, so it is declared after the class.
Index("ix_balobs_acct_date", BalanceObservation.account_id, BalanceObservation.observed_at.desc())
