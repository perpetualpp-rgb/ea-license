"""The frontend's ApiSource tests run against fixtures recorded from this API.

This test rebuilds those fixtures from real responses and fails when they drift, so a change
to the bars/instruments/quote/details/observations contract breaks here before it breaks the app.
All market rows are synthetic (conftest seeders), never real market data.
Regenerate after an intended change: FLOWLAB_UPDATE_FIXTURES=1 python3 -m pytest tests/test_frontend_contract.py
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from conftest import SERVER, seed_futures_state, seed_market, seed_wall_book

FIXTURES = SERVER.parent / "app" / "src" / "test" / "fixtures"
END = datetime(2026, 1, 5, 10, 47, tzinfo=timezone.utc)   # fixed, in the past: same bars on every run
STABLE_ID, STABLE_SYMBOL, STABLE_NOW = 1, "BTCTESTUSDT", "2026-01-05T10:47:00Z"


def _stable(body, iid: int, symbol: str):
    """Replace what differs between runs (ids, random symbol, request time) with fixed values."""
    text = json.dumps(body, sort_keys=True).replace(symbol, STABLE_SYMBOL)
    body = json.loads(text)
    if body.get("instrument_id") == iid:
        body["instrument_id"] = STABLE_ID
    items = body["items"] if "items" in body else [body]
    for d in items:
        if d.get("instrument_id") == iid:
            d["instrument_id"] = STABLE_ID
    for g in body.get("gaps", []):
        g["id"], g["detected_at"] = 1, STABLE_NOW
    body["access"]["data_until"] = STABLE_NOW
    return body


def _check(name: str, body) -> None:
    path = FIXTURES / name
    text = json.dumps(body, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    if os.environ.get("FLOWLAB_UPDATE_FIXTURES") == "1":
        path.write_text(text, encoding="utf-8")
    assert path.exists(), f"{path} missing: run with FLOWLAB_UPDATE_FIXTURES=1"
    assert json.loads(path.read_text(encoding="utf-8")) == body, f"{name} drifted from the API; regenerate it"


def test_recorded_fixtures_match_the_api(client, member, admin, instrument):
    inst = instrument()
    iid, symbol = inst["instrument_id"], inst["symbol"]
    seed_market(admin, inst, minutes=50, end=END)
    # one minute without an aggressor split, so the app must show CVD as unavailable there
    admin.execute("UPDATE klines SET taker_buy_volume = NULL WHERE instrument_id = %s AND open_time = %s",
                  [iid, END - timedelta(minutes=20)])
    admin.execute("INSERT INTO data_quality (instrument_id, stream, source_key, reason, status, gap_start, gap_end)"
                  " VALUES (%s, 'kline', 'contract-test', 'disconnected', 'open', %s, %s)",
                  [iid, END - timedelta(minutes=12), END - timedelta(minutes=10)])
    m = member(realtime=True, admin_conn=admin)

    listed = client.get("/v1/instruments", headers=m.h)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    body["items"] = [d for d in body["items"] if d["instrument_id"] == iid]
    _check("server-instruments.json", _stable(body, iid, symbol))

    bars = client.get(f"/v1/instruments/{iid}/bars", headers=m.h, params={
        "tf": "5m", "source": "kline", "from": (END - timedelta(minutes=47)).isoformat(), "to": END.isoformat()})
    assert bars.status_code == 200, bars.text
    _check("server-bars-5m.json", _stable(bars.json(), iid, symbol))


def test_recorded_market_state_fixtures_match_the_api(client, member, admin, instrument):
    """A perpetual with mark/OI rows, a scripted book (walls + every outcome) and one kline 24 h back,
    so quote/details/observations fixtures carry non-null mark, change_24h, OI and funding."""
    inst = instrument()
    iid, symbol = inst["instrument_id"], inst["symbol"]
    seed_market(admin, inst, minutes=50, end=END, book=False)
    seed_futures_state(admin, inst, END - timedelta(minutes=70), END)
    seed_wall_book(admin, inst, END - timedelta(minutes=30))
    last = admin.execute("SELECT max(event_ts) AS t FROM trades WHERE instrument_id = %s", [iid]).fetchone()["t"]
    admin.execute("INSERT INTO klines (instrument_id, interval_s, open_time, open, high, low, close, volume, trade_count,"
                  " taker_buy_volume) VALUES (%s, 60, %s, 98, 99, 97.5, 98.5, 4.5, 9, 2)",
                  [iid, (last - timedelta(hours=24, minutes=1)).replace(second=0, microsecond=0)])
    admin.execute("INSERT INTO liquidations (instrument_id, event_ts, side, price, avg_price, qty, status)"
                  " VALUES (%s, %s, 'sell', 100.05, 100.04, 0.75, 'FILLED')", [iid, END - timedelta(minutes=7)])
    admin.execute("INSERT INTO data_quality (instrument_id, stream, source_key, reason, status, gap_start, gap_end)"
                  " VALUES (%s, 'trades', 'contract-test-ids', 'id_gap_detected', 'partial', %s, %s)",
                  [iid, END - timedelta(minutes=40), END - timedelta(minutes=39)])
    m = member(realtime=True, admin_conn=admin)

    quote = client.get(f"/v1/instruments/{iid}/quote", headers=m.h)
    assert quote.status_code == 200, quote.text
    assert quote.json()["change_24h"] is not None and quote.json()["mark"] is not None
    _check("server-quote.json", _stable(quote.json(), iid, symbol))

    details = client.get(f"/v1/instruments/{iid}/details", headers=m.h,
                         params={"from": (END - timedelta(hours=1)).isoformat(), "to": END.isoformat()})
    assert details.status_code == 200, details.text
    d = details.json()
    assert d["oi"] is not None and d["funding"] is not None and d["book"]["walls"] and d["book"]["events"]
    _check("server-details.json", _stable(d, iid, symbol))

    obs = client.get(f"/v1/instruments/{iid}/observations", headers=m.h, params={"to": END.isoformat()})
    assert obs.status_code == 200, obs.text
    assert {i["kind"] for i in obs.json()["items"]} >= {"cvd", "oi", "wall", "funding"}
    _check("server-observations.json", _stable(obs.json(), iid, symbol))
