"""Minimal valid-row inserters for every table, used by schema tests.

Each factory inserts one row through SQLAlchemy Core (so the database, not the
ORM, judges it), creating fresh parent rows when none are passed in, and
returns the inserted values. Unique columns get new_id()-based values, so
repeated calls never collide by accident. Pass overrides to make a row invalid
on purpose.

Synthetic data only: nothing here comes from the fixture corpus.
"""

from collections.abc import Callable
from typing import Any, TypeAlias

from sqlalchemy import Connection, Table, func, insert, select

from hisaab.models import Base, new_id, utc_now

Row: TypeAlias = dict[str, Any]  # the values a factory inserted

T: dict[str, Table] = dict(Base.metadata.tables)


def _insert(conn: Connection, table: str, defaults: Row, overrides: Row) -> Row:
    values: Row = {**defaults, **overrides}
    conn.execute(insert(T[table]).values(**values))
    return values


def user(conn: Connection, **kw: Any) -> Row:
    return _insert(conn, "users", {
        "id": new_id(), "username": f"user-{new_id()}", "password_hash": "not-a-real-hash",
        "display_name": "Test User", "role": "member",
    }, kw)


def account(conn: Connection, user_id: str | None = None, **kw: Any) -> Row:
    user_id = user_id or user(conn)["id"]
    return _insert(conn, "accounts", {
        "id": new_id(), "user_id": user_id, "type": "bank", "display_name": "Test Bank",
        "last4": new_id()[:4], "opening_balance": 0, "opening_balance_date": "2026-07-01",
    }, kw)


def account_identifier(conn: Connection, account_id: str | None = None, user_id: str | None = None, **kw: Any) -> Row:
    if account_id is None:
        acct: Row = account(conn, user_id)
        account_id, user_id = acct["id"], acct["user_id"]
    return _insert(conn, "account_identifiers", {
        "id": new_id(), "account_id": account_id, "user_id": user_id, "kind": "last4",
        "identifier": new_id()[:8], "origin": "account_setup",
    }, kw)


def source(conn: Connection, user_id: str | None = None, **kw: Any) -> Row:
    user_id = user_id or user(conn)["id"]
    next_seq: int | None = conn.execute(select(func.coalesce(func.max(T["sources"].c.ingest_seq), 0) + 1)).scalar()
    now: str = utc_now()
    return _insert(conn, "sources", {
        "id": new_id(), "ingest_seq": next_seq, "user_id": user_id, "kind": "email",
        "content_hash": new_id(), "external_message_id": new_id(), "received_at": now,
        "ingested_at": now, "processing_state": "parsed",
    }, kw)


def payee(conn: Connection, **kw: Any) -> Row:
    return _insert(conn, "payees", {"id": new_id(), "normalized_name": f"Payee {new_id()}", "type": "merchant"}, kw)


def category(conn: Connection, user_id: str | None = None, **kw: Any) -> Row:
    user_id = user_id or user(conn)["id"]
    return _insert(conn, "categories", {"id": new_id(), "user_id": user_id, "name": f"Cat {new_id()}"}, kw)


def transaction(conn: Connection, user_id: str | None = None, source_id: str | None = None, **kw: Any) -> Row:
    user_id = user_id or user(conn)["id"]
    source_id = source_id or source(conn, user_id)["id"]
    return _insert(conn, "transactions", {
        "id": new_id(), "user_id": user_id, "derived_key": new_id(), "amount": 10000,
        "kind": "expense", "transaction_date": "2026-08-20", "status": "confirmed",
        "primary_source_id": source_id,
    }, kw)


def transaction_source(
    conn: Connection, transaction_id: str | None = None, source_id: str | None = None, **kw: Any,
) -> Row:
    if transaction_id is None:
        txn: Row = transaction(conn)
        transaction_id, source_id = txn["id"], source_id or txn["primary_source_id"]
    return _insert(conn, "transaction_sources", {"transaction_id": transaction_id, "source_id": source_id}, kw)


def transaction_edit(conn: Connection, transaction_id: str | None = None, user_id: str | None = None,
                     field: str = "amount", new_value: str | None = "25000", **kw: Any) -> Row:
    if transaction_id is None:
        txn: Row = transaction(conn)
        transaction_id, user_id = txn["id"], txn["user_id"]
    return _insert(conn, "transaction_edits", {
        "id": new_id(), "transaction_id": transaction_id, "user_id": user_id,
        "field": field, "new_value": new_value,
    }, kw)


def payee_category(conn: Connection, **kw: Any) -> Row:
    u: str = user(conn)["id"]
    return _insert(conn, "payee_categories", {
        "user_id": u, "payee_id": payee(conn)["id"], "category_id": category(conn, u)["id"],
    }, kw)


def balance_observation(conn: Connection, **kw: Any) -> Row:
    acct: Row = account(conn)
    return _insert(conn, "balance_observations", {
        "id": new_id(), "user_id": acct["user_id"], "account_id": acct["id"],
        "balance": 8420000, "observed_at": "2026-08-12", "source": "manual",
    }, kw)


def balance_delta(conn: Connection, **kw: Any) -> Row:
    obs: Row = balance_observation(conn)
    return _insert(conn, "balance_deltas", {
        "id": new_id(), "user_id": obs["user_id"], "account_id": obs["account_id"],
        "computed_balance": 8000000, "observed_balance": 8420000, "delta": 420000,
        "arose_from": "2026-07-01", "observation_id": obs["id"], "status": "open",
    }, kw)


def parser_template(conn: Connection, **kw: Any) -> Row:
    return _insert(conn, "parser_templates", {
        "id": new_id(), "artifact_type": "email", "name": "Test template", "spec": "{}",
        "origin": "human", "status": "active",
    }, kw)


def extraction_cache(conn: Connection, **kw: Any) -> Row:
    return _insert(conn, "extraction_cache", {
        "content_hash": new_id(), "outcome": "transaction", "model": "test-model", "tier": 2,
    }, kw)


def format_sighting(conn: Connection, **kw: Any) -> Row:
    now: str = utc_now()
    return _insert(conn, "format_sightings", {
        "id": new_id(), "fingerprint": new_id(), "sender_address": "alerts@example.test",
        "subject_masked": "Txn of ₹#", "structural_markers": "[]", "example_source_ids": "[]",
        "state": "accumulating", "first_seen_at": now, "last_seen_at": now,
    }, kw)


def normalization_pattern(conn: Connection, **kw: Any) -> Row:
    return _insert(conn, "normalization_patterns", {
        "id": new_id(), "match_type": "contains", "pattern": "EXAMPLE", "payee_id": payee(conn)["id"],
        "origin": "seed",
    }, kw)


def sender(conn: Connection, **kw: Any) -> Row:
    return _insert(conn, "senders", {
        "id": new_id(), "email_address": f"{new_id()}@example.test", "status": "active",
    }, kw)


def sender_candidate(conn: Connection, **kw: Any) -> Row:
    return _insert(conn, "sender_candidates", {
        "id": new_id(), "discovered_by_user_id": user(conn)["id"],
        "sender_address": f"{new_id()}@example.test", "matched_subjects": "[]", "match_count": 1,
        "first_seen_at": utc_now(),
    }, kw)


def gmail_connection(conn: Connection, **kw: Any) -> Row:
    now: str = utc_now()
    return _insert(conn, "gmail_connections", {
        "user_id": user(conn)["id"], "email_address": "someone@example.test",
        "refresh_token_enc": b"\x00encrypted", "status": "connected", "connected_at": now, "updated_at": now,
    }, kw)


def statement_staging(conn: Connection, **kw: Any) -> Row:
    return _insert(conn, "statement_staging", {
        "id": new_id(), "user_id": user(conn)["id"], "filename": "statement.pdf",
        "content_hash": new_id(), "parsed_rows": "[]", "row_dispositions": "[]",
        "validation_result": "{}", "expires_at": utc_now(),
    }, kw)


def job(conn: Connection, **kw: Any) -> Row:
    return _insert(conn, "jobs", {"id": new_id(), "kind": "sync", "state": "queued"}, kw)


def session(conn: Connection, **kw: Any) -> Row:
    return _insert(conn, "sessions", {"id": new_id(), "user_id": user(conn)["id"], "last_seen_at": utc_now()}, kw)


def setting(conn: Connection, **kw: Any) -> Row:
    return _insert(conn, "settings", {"key": f"test_{new_id()}", "value": "1", "updated_at": utc_now()}, kw)


FACTORIES: dict[str, Callable[..., Row]] = {
    "users": user, "accounts": account, "account_identifiers": account_identifier,
    "sources": source, "payees": payee, "categories": category, "transactions": transaction,
    "transaction_sources": transaction_source, "transaction_edits": transaction_edit,
    "payee_categories": payee_category, "balance_observations": balance_observation,
    "balance_deltas": balance_delta, "parser_templates": parser_template,
    "extraction_cache": extraction_cache, "format_sightings": format_sighting,
    "normalization_patterns": normalization_pattern, "senders": sender,
    "sender_candidates": sender_candidate, "gmail_connections": gmail_connection,
    "statement_staging": statement_staging, "jobs": job, "sessions": session, "settings": setting,
}
