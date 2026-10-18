"""Migrations, roles, RLS coverage and immutability, checked directly in SQL."""
from __future__ import annotations

import shutil
import uuid

import psycopg
import pytest

import migrate
from app.config import ConfigError
from app.db import Database
from conftest import _connect, seed_market

PER_USER_TABLES = {"users", "entitlements", "idempotency_records", "market_snapshots", "personal_trades", "fills",
                   "cashflows", "journal_entries", "journal_revisions", "deletion_tombstones", "alert_rules",
                   "alert_events", "alert_deliveries"}


def test_migration_applied_once_and_second_run_is_noop(dsns, admin):
    assert dsns["applied"] == ["0001_init"]
    assert migrate.apply_migrations(dsns["admin"]) == []
    assert admin.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()["n"] == 1


def test_fresh_database_migrates_twice_cleanly(dsns):
    pg = dsns["cluster"]
    name = "flowlab_twice_" + uuid.uuid4().hex[:6]
    with psycopg.connect(pg.dsn(), autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    dsn = pg.dsn(dbname=name)
    # roles already exist cluster-wide from the main test database: the DO block must tolerate that
    assert migrate.apply_migrations(dsn) == ["0001_init"]
    assert migrate.apply_migrations(dsn) == []
    assert any("applied" in line for line in migrate.status(dsn))


def test_edited_migration_is_refused(dsns, tmp_path):
    d = tmp_path / "migrations"
    shutil.copytree(migrate.MIGRATIONS, d)
    f = d / "0001_init.sql"
    f.write_text(f.read_text(encoding="utf-8") + "\n-- edited\n", encoding="utf-8")
    with pytest.raises(migrate.MigrationError, match="changed after it was applied"):
        migrate.apply_migrations(dsns["admin"], d)


def test_every_per_user_table_has_rls(admin):
    owner_tables = {r["table_name"] for r in admin.execute(
        "SELECT table_name FROM information_schema.columns WHERE table_schema = 'flowlab' AND column_name = 'owner_id'")}
    assert owner_tables | {"users"} == PER_USER_TABLES
    rows = admin.execute("SELECT relname, relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                         " WHERE n.nspname = 'flowlab' AND relname = ANY(%s)", [list(PER_USER_TABLES)]).fetchall()
    assert {r["relname"] for r in rows if r["relrowsecurity"]} == PER_USER_TABLES
    policies = {r["tablename"] for r in admin.execute("SELECT tablename FROM pg_policies WHERE schemaname = 'flowlab'")}
    assert policies == PER_USER_TABLES


def test_api_role_is_not_privileged(admin):
    r = admin.execute("SELECT rolsuper, rolbypassrls, pg_has_role('flowlab_api', 'flowlab_app', 'MEMBER') AS member"
                      " FROM pg_roles WHERE rolname = 'flowlab_api'").fetchone()
    assert r == {"rolsuper": False, "rolbypassrls": False, "member": True}
    owners = {r["tableowner"] for r in admin.execute("SELECT tableowner FROM pg_tables WHERE schemaname = 'flowlab'")}
    assert "flowlab_api" not in owners and "flowlab_app" not in owners


def test_api_refuses_to_run_as_owner(dsns):
    db = Database(dsns["admin"], 1, 1)
    db.open()
    try:
        with pytest.raises(ConfigError, match="RLS would not apply"):
            db.check_role()
    finally:
        db.close()


def test_ingest_role_cannot_touch_member_data(dsns):
    conn = _connect(dsns["ingest"])
    try:
        for table in ("journal_entries", "personal_trades", "users", "market_snapshots"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"SELECT count(*) FROM {table}")
    finally:
        conn.close()


def _snapshot_row(admin, owner_id, iid):
    sid = uuid.uuid4()
    admin.execute("INSERT INTO market_snapshots (id, owner_id, instrument_id, data_access, metric_values, engine_version,"
                  " schema_version, quality_flags, chart_viewport) VALUES (%s, %s, %s, 'delayed', '{}', 'x', 1, '{}', '{}')",
                  [sid, owner_id, iid])
    return sid


def test_snapshots_reject_update_delete_truncate(admin, app_conn, member, instrument):
    m = member()
    iid = instrument()["instrument_id"]
    sid = _snapshot_row(admin, m.id, iid)
    # the API role has no UPDATE/DELETE privilege at all
    with app_conn.transaction():
        app_conn.execute("SELECT set_config('app.user_id', %s, true)", [str(m.id)])
        assert app_conn.execute("SELECT count(*) AS n FROM market_snapshots WHERE id = %s", [sid]).fetchone()["n"] == 1
    for sql in ("UPDATE market_snapshots SET engine_version = 'changed' WHERE id = %s",
                "DELETE FROM market_snapshots WHERE id = %s"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with app_conn.transaction():
                app_conn.execute("SELECT set_config('app.user_id', %s, true)", [str(m.id)])
                app_conn.execute(sql, [sid])
    # even the owner is stopped by the trigger
    for sql in ("UPDATE market_snapshots SET engine_version = 'changed' WHERE id = %s",
                "DELETE FROM market_snapshots WHERE id = %s"):
        with pytest.raises(psycopg.errors.RestrictViolation, match="immutable"):
            admin.execute(sql, [sid])
    with pytest.raises(psycopg.errors.RestrictViolation):
        admin.execute("TRUNCATE market_snapshots CASCADE")
    assert admin.execute("SELECT engine_version FROM market_snapshots WHERE id = %s", [sid]).fetchone()["engine_version"] == "x"


def test_retention_purge_needs_flag_and_owner(admin, app_conn, member, instrument):
    m = member()
    sid = _snapshot_row(admin, m.id, instrument()["instrument_id"])
    # the API role cannot use the purge flag
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with app_conn.transaction():
            app_conn.execute("SELECT set_config('app.user_id', %s, true)", [str(m.id)])
            app_conn.execute("SELECT set_config('flowlab.retention_purge', 'on', true)")
            app_conn.execute("DELETE FROM market_snapshots WHERE id = %s", [sid])
    # account deletion run by the owner with the flag removes the member and everything they own
    with admin.transaction():
        admin.execute("SELECT set_config('flowlab.retention_purge', 'on', true)")
        admin.execute("DELETE FROM users WHERE id = %s", [m.id])
    assert admin.execute("SELECT count(*) AS n FROM market_snapshots WHERE id = %s", [sid]).fetchone()["n"] == 0


def test_raw_events_and_fills_are_append_only(admin, instrument):
    iid = instrument()["instrument_id"]
    run = uuid.uuid4()
    admin.execute("INSERT INTO ingestion_runs (run_id, tool, tool_version, source_root) VALUES (%s, 't', '1', '/x')", [run])
    eid = uuid.uuid4()
    admin.execute("INSERT INTO raw_events (event_id, instrument_id, stream, schema_version, payload, ingestion_run_id)"
                  " VALUES (%s, %s, 'meta', 1, '[1, \"connected\"]', %s)", [eid, iid, run])
    with pytest.raises(psycopg.errors.RestrictViolation):
        admin.execute("UPDATE raw_events SET stream = 'x' WHERE event_id = %s", [eid])
    with pytest.raises(psycopg.errors.RestrictViolation):
        admin.execute("DELETE FROM raw_events WHERE event_id = %s", [eid])


def test_market_tables_are_read_only_for_api(app_conn, admin, instrument):
    inst = instrument()
    seed_market(admin, inst, minutes=2)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("DELETE FROM trades WHERE instrument_id = %s", [inst["instrument_id"]])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("UPDATE instruments SET tick_size = 1 WHERE instrument_id = %s", [inst["instrument_id"]])


def test_instrument_spec_change_bumps_metadata_version(admin, instrument):
    inst = instrument()
    assert inst["metadata_version"] == 1
    row = admin.execute("UPDATE instruments SET tick_size = 0.1 WHERE instrument_id = %s RETURNING metadata_version",
                        [inst["instrument_id"]]).fetchone()
    assert row["metadata_version"] == 2
