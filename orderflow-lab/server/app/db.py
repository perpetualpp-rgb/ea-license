"""Connection pool and per-request transactions.

Every request that touches per-user data runs inside ``Database.user_tx(principal)``:

    BEGIN;
    SELECT set_config('app.user_id', '<uuid>', true);   -- = SET LOCAL app.user_id, but parameterised
    INSERT INTO users ... ON CONFLICT DO NOTHING;        -- just-in-time provisioning
    ... queries (RLS narrows every one of them to the caller) ...
    COMMIT;

``set_config(..., true)`` is transaction-local, so the setting disappears at COMMIT or
ROLLBACK and a pooled connection never carries one user's id into another request.
The commit happens before the endpoint returns, so a 2xx response means the data is on
the server.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import ConfigError

CONNECT_OPTIONS = "-c search_path=flowlab -c timezone=UTC"


def connect_kwargs() -> dict:
    return {"autocommit": True, "row_factory": dict_row, "options": CONNECT_OPTIONS}


class Database:
    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 10):
        self.pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size, kwargs=connect_kwargs(),
                                   open=False, name="flowlab")

    def open(self) -> None:
        self.pool.open(wait=True, timeout=15)

    def close(self) -> None:
        self.pool.close()

    def check_role(self) -> None:
        """Refuse to serve through a role that RLS would not restrict."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT r.rolsuper, r.rolbypassrls, pg_has_role(current_user, 'flowlab_app', 'MEMBER') AS is_app,"
                " EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'flowlab' AND tablename = 'journal_entries'"
                "         AND pg_has_role(current_user, tableowner, 'MEMBER')) AS owns"
                " FROM pg_roles r WHERE r.rolname = current_user").fetchone()
        if row is None or row["rolsuper"] or row["rolbypassrls"] or row["owns"] or not row["is_app"]:
            raise ConfigError("FLOWLAB_DATABASE_URL must use a login role that is a member of flowlab_app, "
                              "not a superuser, not BYPASSRLS and not the table owner (RLS would not apply)")

    @contextmanager
    def tx(self) -> Iterator[psycopg.Connection]:
        """Transaction without a user (market data only: per-user tables return nothing)."""
        with self.pool.connection() as conn:
            with conn.transaction():
                yield conn

    @contextmanager
    def user_tx(self, user_id: UUID, issuer: Optional[str] = None, subject: Optional[str] = None,
                email: Optional[str] = None) -> Iterator[psycopg.Connection]:
        with self.pool.connection() as conn:
            with conn.transaction():
                set_user(conn, user_id)
                if issuer is not None and subject is not None:
                    conn.execute(
                        "INSERT INTO users (id, issuer, subject, email) VALUES (%s, %s, %s, %s)"
                        " ON CONFLICT (id) DO NOTHING", [user_id, issuer, subject, email])
                yield conn


def set_user(conn: psycopg.Connection, user_id: UUID) -> None:
    conn.execute("SELECT set_config('app.user_id', %s, true)", [str(user_id)])
