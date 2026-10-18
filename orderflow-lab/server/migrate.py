#!/usr/bin/env python3
"""Apply server/migrations/*.sql in order, each exactly once.

    FLOWLAB_ADMIN_DATABASE_URL=postgresql://owner@127.0.0.1:5432/flowlab python3 migrate.py
    python3 migrate.py --dsn postgresql://... --status

Applied files are recorded in flowlab.schema_migrations with a SHA-256 checksum. A file
that changed after it was applied is an error (write a new migration instead). The run
holds an advisory lock so two deploys cannot migrate at the same time.
Connect as the database owner, never as the API role.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import List

import psycopg

HERE = Path(__file__).resolve().parent
MIGRATIONS = HERE / "migrations"
LOCK_ID = 0x464C4F574C4142  # "FLOWLAB"


class MigrationError(RuntimeError):
    pass


def migration_files(directory: Path = MIGRATIONS) -> List[Path]:
    return sorted(p for p in directory.glob("*.sql") if p.name[:4].isdigit())


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_migrations(dsn: str, directory: Path = MIGRATIONS) -> List[str]:
    """Apply pending migrations; returns the versions applied by this call."""
    done: List[str] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", [LOCK_ID])
            conn.execute("CREATE SCHEMA IF NOT EXISTS flowlab")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS flowlab.schema_migrations ("
                " version text PRIMARY KEY, checksum text NOT NULL,"
                " applied_at timestamptz NOT NULL DEFAULT now())"
            )
            applied = dict(conn.execute("SELECT version, checksum FROM flowlab.schema_migrations").fetchall())
            for path in migration_files(directory):
                version, digest = path.stem, checksum(path)
                if version in applied:
                    if applied[version] != digest:
                        raise MigrationError(
                            f"{path.name} changed after it was applied (recorded {applied[version][:12]}, "
                            f"file {digest[:12]}); add a new migration instead of editing this one")
                    continue
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO flowlab.schema_migrations (version, checksum) VALUES (%s, %s)",
                             [version, digest])
                done.append(version)
    return done


def status(dsn: str, directory: Path = MIGRATIONS) -> List[str]:
    with psycopg.connect(dsn) as conn:
        try:
            applied = dict(conn.execute("SELECT version, applied_at FROM flowlab.schema_migrations").fetchall())
        except psycopg.errors.UndefinedTable:
            applied = {}
    lines = []
    for path in migration_files(directory):
        at = applied.get(path.stem)
        lines.append(f"{path.stem:30s} {'applied ' + at.isoformat() if at else 'pending'}")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="apply Flow Lab database migrations")
    ap.add_argument("--dsn", default=os.environ.get("FLOWLAB_ADMIN_DATABASE_URL"),
                    help="owner connection (default: $FLOWLAB_ADMIN_DATABASE_URL)")
    ap.add_argument("--status", action="store_true", help="list migrations and exit")
    args = ap.parse_args(argv)
    if not args.dsn:
        ap.error("set FLOWLAB_ADMIN_DATABASE_URL or pass --dsn")
    if args.status:
        print("\n".join(status(args.dsn)))
        return 0
    try:
        applied = apply_migrations(args.dsn)
    except MigrationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print("applied: " + ", ".join(applied) if applied else "nothing to apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
