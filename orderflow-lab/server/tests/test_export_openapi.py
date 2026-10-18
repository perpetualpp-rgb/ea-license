"""Export of own data, and the OpenAPI contract staying in sync with the code."""
from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import timedelta

from app.openapi_export import TARGET, render
from conftest import now_utc


def test_export_json_and_csv_contain_own_data_with_versions(client, member):
    m = member()
    h = {**m.h, "Idempotency-Key": str(uuid.uuid4())}
    entry = client.post("/v1/journal/entries", headers=h,
                        json={"title": "=HYPERLINK(\"http://evil\")", "facts": "ทองแตะแนวต้าน", "tags": ["xau"]}).json()
    client.patch(f"/v1/journal/entries/{entry['id']}", headers=m.h, json={"expected_revision": 1, "lessons": "รอ"})
    trade = client.post("/v1/trades", headers=m.h, json={
        "venue_label": "broker", "symbol": "XAUUSD", "market_type": "cfd", "contract_type": "linear",
        "contract_multiplier": "100", "quote_currency": "USD", "settlement_currency": "USD", "side": "long",
        "funding_status": "not_applicable"}).json()
    for minute, action, price in ((0, "entry", "2000"), (5, "exit", "2010")):
        client.post(f"/v1/trades/{trade['id']}/fills", headers=m.h, json={
            "ts": (now_utc() - timedelta(hours=1, minutes=-minute)).isoformat(), "action": action,
            "quantity": "0.5", "price": price, "fee": "1", "fee_currency": "USD"})

    r = client.get("/v1/export", headers=m.h, params={"format": "json"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.headers["content-disposition"].startswith('attachment; filename="flowlab-export-')
    doc = json.loads(r.text)
    assert doc["export_schema_version"] == 1 and doc["owner_id"] == str(m.id)
    [e] = doc["journal_entries"]
    assert [v["version"] for v in e["revisions"]] == [1, 2] and e["revision"] == 2
    [t] = doc["personal_trades"]
    assert t["summary"]["net_pnl"] == "498" and len(t["fills"]) == 2     # 0.5 x 10 x 100 - 2 fees

    j = client.get("/v1/export", headers=m.h, params={"format": "csv", "dataset": "journal_entries"})
    assert j.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(j.text.lstrip("﻿"))))
    assert len(rows) == 1 and rows[0]["title"].startswith("'=")           # formula neutralised
    assert rows[0]["facts"] == "ทองแตะแนวต้าน" and rows[0]["tags"] == "xau"
    tr = list(csv.DictReader(io.StringIO(client.get("/v1/export", headers=m.h, params={
        "format": "csv", "dataset": "trades"}).text.lstrip("﻿"))))
    assert tr[0]["net_pnl"] == "498" and tr[0]["complete"] == "True" and tr[0]["contract_multiplier"] == "100"
    fl = list(csv.DictReader(io.StringIO(client.get("/v1/export", headers=m.h, params={
        "format": "csv", "dataset": "fills"}).text.lstrip("﻿"))))
    assert [f["action"] for f in fl] == ["entry", "exit"]
    # date range: nothing created in the future
    future = client.get("/v1/export", headers=m.h, params={"format": "json",
                                                           "from": (now_utc() + timedelta(days=1)).isoformat()})
    assert json.loads(future.text)["journal_entries"] == [] and json.loads(future.text)["personal_trades"] == []


def test_openapi_json_is_in_sync_with_the_app():
    assert TARGET.exists(), "run: python3 -m app.openapi_export"
    assert TARGET.read_text(encoding="utf-8") == render(), "openapi.json is stale: run python3 -m app.openapi_export"


def test_openapi_covers_the_contract_and_requires_bearer_auth():
    spec = json.loads(render())
    paths = spec["paths"]
    for p in ("/v1/me", "/v1/instruments", "/v1/instruments/{instrument_id}/bars", "/v1/instruments/{instrument_id}/quality",
              "/v1/instruments/{instrument_id}/quote", "/v1/instruments/{instrument_id}/details",
              "/v1/instruments/{instrument_id}/observations",
              "/v1/journal/entries", "/v1/journal/entries/{entry_id}", "/v1/journal/entries/{entry_id}/revisions",
              "/v1/journal/snapshots", "/v1/trades", "/v1/trades/{trade_id}/fills", "/v1/trades/{trade_id}/cashflows",
              "/v1/trades/stats", "/v1/alerts/rules", "/v1/alerts/rules/{rule_id}", "/v1/alerts/events",
              "/v1/alerts/events/{event_id}/ack", "/v1/export"):
        assert p in paths, p
    for path, ops in paths.items():
        for method, op in ops.items():
            if path.startswith("/v1"):
                assert op.get("security") == [{"HTTPBearer": []}], (method, path)
    params = {p["name"] for p in paths["/v1/journal/entries"]["post"]["parameters"]}
    assert "idempotency-key" in params
    assert spec["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert not any("token" in p["name"].lower() for ops in paths.values() for op in ops.values()
                   for p in op.get("parameters", []) if p["in"] == "query")
