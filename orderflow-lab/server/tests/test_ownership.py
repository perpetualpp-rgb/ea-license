"""Member A cannot read, change, delete or export member B's data: through the API, and
directly in SQL as the API role (RLS), including without app.user_id."""
from __future__ import annotations

import time
import uuid

import jwt
import psycopg
import pytest

from app.devtoken import mint


def _setup_a(client, a, instrument):
    marker = f"secret-of-a-{uuid.uuid4().hex[:8]}"
    entry = client.post("/v1/journal/entries", headers={**a.h, "Idempotency-Key": str(uuid.uuid4())},
                        json={"title": marker, "facts": marker}).json()
    trade = client.post("/v1/trades", headers=a.h, json={
        "venue_label": "broker-x", "symbol": "XAUUSD", "market_type": "cfd", "contract_type": "linear",
        "contract_multiplier": "100", "quote_currency": "USD", "settlement_currency": "USD", "side": "long",
        "notes": marker}).json()
    rule = client.post("/v1/alerts/rules", headers=a.h, json={
        "instrument_id": instrument()["instrument_id"], "metric": "oi_change_pct", "threshold": "2",
        "threshold_unit": "pct"}).json()
    return marker, entry, trade, rule


def test_member_b_cannot_reach_member_a_through_the_api(client, member, instrument, admin):
    a, b = member(), member()
    marker, entry, trade, rule = _setup_a(client, a, instrument)
    eid, tid, rid = entry["id"], trade["id"], rule["id"]

    assert client.get(f"/v1/journal/entries/{eid}", headers=b.h).status_code == 404
    assert client.get(f"/v1/journal/entries/{eid}/revisions", headers=b.h).status_code == 404
    assert client.patch(f"/v1/journal/entries/{eid}", headers=b.h,
                        json={"expected_revision": 1, "title": "hijacked"}).status_code == 404
    assert client.delete(f"/v1/journal/entries/{eid}", headers=b.h).status_code == 404
    assert client.get("/v1/journal/entries", headers=b.h).json()["items"] == []

    assert client.get(f"/v1/trades/{tid}", headers=b.h).status_code == 404
    assert client.patch(f"/v1/trades/{tid}", headers=b.h, json={"notes": "x"}).status_code == 404
    assert client.post(f"/v1/trades/{tid}/fills", headers=b.h, json={
        "ts": "2026-10-01T00:00:00Z", "action": "entry", "quantity": "1", "price": "1"}).status_code == 404
    assert client.delete(f"/v1/trades/{tid}", headers=b.h).status_code == 404
    assert client.get("/v1/trades", headers=b.h).json()["items"] == []
    assert client.get("/v1/trades/stats", headers=b.h).json()["counts"]["closed"] == 0

    assert client.get(f"/v1/alerts/rules/{rid}", headers=b.h).status_code == 404
    assert client.patch(f"/v1/alerts/rules/{rid}", headers=b.h, json={"enabled": False}).status_code == 404
    assert client.delete(f"/v1/alerts/rules/{rid}", headers=b.h).status_code == 404
    assert client.get("/v1/alerts/rules", headers=b.h).json() == []

    for params in ({"format": "json"}, {"format": "csv", "dataset": "journal_entries"},
                   {"format": "csv", "dataset": "trades"}, {"format": "csv", "dataset": "fills"}):
        r = client.get("/v1/export", headers=b.h, params=params)
        assert r.status_code == 200 and marker not in r.text and eid not in r.text and tid not in r.text

    # A still has everything, unchanged
    assert client.get(f"/v1/journal/entries/{eid}", headers=a.h).json()["title"] == marker
    assert client.get(f"/v1/trades/{tid}", headers=a.h).status_code == 200
    assert client.get(f"/v1/alerts/rules/{rid}", headers=a.h).json()["enabled"] is True
    assert marker in client.get("/v1/export", headers=a.h).text


def _as(conn: psycopg.Connection, user_id):
    conn.execute("SELECT set_config('app.user_id', %s, true)", [str(user_id)])


def test_rls_enforced_in_sql_for_the_api_role(client, member, instrument, app_conn):
    a, b = member(), member()
    marker, entry, trade, _ = _setup_a(client, a, instrument)
    eid, tid = entry["id"], trade["id"]
    tables = ["journal_entries", "journal_revisions", "personal_trades", "alert_rules", "idempotency_records", "users"]

    # no app.user_id at all: nothing is visible, even with no WHERE clause
    for t in tables:
        assert app_conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] == 0, t
    # app.user_id of B: A's rows are invisible and untouchable
    with app_conn.transaction():
        _as(app_conn, b.id)
        assert app_conn.execute("SELECT count(*) AS n FROM journal_entries WHERE id = %s", [eid]).fetchone()["n"] == 0
        assert app_conn.execute("UPDATE journal_entries SET title = 'x' WHERE id = %s", [eid]).rowcount == 0
        assert app_conn.execute("DELETE FROM journal_entries WHERE id = %s", [eid]).rowcount == 0
        assert app_conn.execute("UPDATE personal_trades SET notes = 'x' WHERE id = %s", [tid]).rowcount == 0
        assert app_conn.execute("DELETE FROM personal_trades WHERE id = %s", [tid]).rowcount == 0
        assert app_conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 1   # only B's own row
    # B cannot write rows that claim to belong to A
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
        with app_conn.transaction():
            _as(app_conn, b.id)
            app_conn.execute("INSERT INTO journal_entries (id, owner_id, entry_type, title) VALUES (%s, %s, 'observation', 'x')",
                             [uuid.uuid4(), a.id])
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
        with app_conn.transaction():
            _as(app_conn, b.id)
            own = uuid.uuid4()
            app_conn.execute("INSERT INTO journal_entries (id, owner_id, entry_type, title) VALUES (%s, %s, 'observation', 'y')",
                             [own, b.id])
            # moving B's own row into A's account is refused by the WITH CHECK clause
            app_conn.execute("UPDATE journal_entries SET owner_id = %s WHERE id = %s", [a.id, own])
    # the setting is transaction-local: the next transaction on this pooled connection sees nothing again
    with app_conn.transaction():
        _as(app_conn, a.id)
        assert app_conn.execute("SELECT title FROM journal_entries WHERE id = %s", [eid]).fetchone()["title"] == marker
    assert app_conn.execute("SELECT count(*) AS n FROM journal_entries").fetchone()["n"] == 0
    assert app_conn.execute("SELECT current_setting('app.user_id', true) AS v").fetchone()["v"] == ""


def test_authentication_is_required_and_strict(client, settings, member):
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me").headers["WWW-Authenticate"].startswith("Bearer")
    assert client.get("/v1/me", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401
    now = int(time.time())
    base = {"sub": "x", "iss": settings.jwt_issuer, "aud": settings.jwt_audience, "iat": now, "exp": now + 60}
    forged = jwt.encode(base, "another-secret-another-secret-123456", algorithm="HS256")
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    expired = jwt.encode({**base, "exp": now - 3600, "iat": now - 7200}, settings.jwt_secret, algorithm="HS256")
    r = client.get("/v1/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "token_expired"
    wrong_aud = jwt.encode({**base, "aud": "someone-else"}, settings.jwt_secret, algorithm="HS256")
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {wrong_aud}"}).status_code == 401
    unsigned = jwt.encode(base, None, algorithm="none")
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {unsigned}"}).status_code == 401
    # tokens in the URL are refused outright
    tok = mint(settings, "url-user")
    r = client.get("/v1/me", params={"access_token": tok})
    assert r.status_code == 400 and r.json()["error"]["code"] == "credentials_in_url" and tok not in r.text
    m = member()
    r = client.get("/v1/me", headers=m.h)
    assert r.json()["id"] == str(m.id) and r.json()["market_data"]["level"] == "delayed"
    assert r.headers["Cache-Control"] == "no-store, private"


def test_access_log_has_no_credentials(client, member, caplog):
    m = member()
    with caplog.at_level("INFO", logger="flowlab.access"):
        client.get("/v1/me", headers=m.h)
        client.get("/v1/me", params={"token": "secret-value-123"})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "/v1/me" in text and m.token not in text and "secret-value-123" not in text
