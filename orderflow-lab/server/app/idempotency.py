"""Idempotency-Key handling for create endpoints.

    claim()  -> None          new key: create the resource, then bind() it (same transaction)
             -> resource_id   same key, same body: replay, return the existing resource
             -> ApiError 409  same key, different body

The claim is an INSERT ... ON CONFLICT DO NOTHING on (owner_id, scope, key); a second
concurrent request with the same key waits on the unique index until the first commits,
then sees the committed record, so a retry storm still creates exactly one resource.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

import psycopg

from .errors import ApiError

HEADER_DESCRIPTION = ("Client-generated unique key (8-200 chars, e.g. a UUID) reused on every retry of the same "
                      "request; a retry returns the original result instead of creating a duplicate")


def check_key(key: Optional[str], required: bool) -> Optional[str]:
    if key is None:
        if required:
            raise ApiError(400, "idempotency_key_required", "Idempotency-Key header is required")
        return None
    key = key.strip()
    if not 8 <= len(key) <= 200 or not key.isprintable():
        raise ApiError(400, "invalid_idempotency_key", "Idempotency-Key must be 8-200 printable characters")
    return key


def claim(conn: psycopg.Connection, owner_id: UUID, scope: str, key: str, request_hash: str) -> Optional[UUID]:
    row = conn.execute(
        "INSERT INTO idempotency_records (owner_id, scope, idem_key, request_hash) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (owner_id, scope, idem_key) DO NOTHING RETURNING idem_key",
        [owner_id, scope, key, request_hash]).fetchone()
    if row is not None:
        return None
    existing = conn.execute(
        "SELECT request_hash, resource_id FROM idempotency_records WHERE owner_id = %s AND scope = %s AND idem_key = %s",
        [owner_id, scope, key]).fetchone()
    if existing is None:  # pragma: no cover - only if the row vanished between the two statements
        raise ApiError(409, "idempotency_conflict", "retry the request")
    if existing["request_hash"] != request_hash:
        raise ApiError(409, "idempotency_key_reused",
                       "this Idempotency-Key was already used with a different request body")
    if existing["resource_id"] is None:  # pragma: no cover - the first request is still in flight
        raise ApiError(409, "idempotency_in_progress", "the original request is still being processed")
    return existing["resource_id"]


def bind(conn: psycopg.Connection, owner_id: UUID, scope: str, key: str, resource_id: UUID) -> None:
    conn.execute("UPDATE idempotency_records SET resource_id = %s WHERE owner_id = %s AND scope = %s AND idem_key = %s",
                 [resource_id, owner_id, scope, key])
