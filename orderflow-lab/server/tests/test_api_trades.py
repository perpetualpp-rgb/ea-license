"""Personal trades through the API: fills, corrections, import keys, cashflows, stats."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

T0 = datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc)


def _trade(client, m, **kw):
    body = {"venue_label": "binance", "symbol": "BTCUSDT", "market_type": "perpetual", "contract_type": "linear",
            "contract_multiplier": "1", "quote_currency": "USDT", "settlement_currency": "USDT", "side": "long",
            "funding_status": "recorded", **kw}
    r = client.post("/v1/trades", json=body, headers=m.h)
    assert r.status_code == 201, r.text
    return r.json()


def _fill(client, m, tid, minute, action, qty, price, fee="0", **kw):
    return client.post(f"/v1/trades/{tid}/fills", headers=m.h, json={
        "ts": (T0 + timedelta(minutes=minute)).isoformat(), "action": action, "quantity": qty, "price": price,
        "fee": fee, "fee_currency": kw.pop("fee_currency", "USDT"), **kw})


def test_contract_spec_is_required_or_copied(client, member, instrument):
    m = member()
    r = client.post("/v1/trades", headers=m.h, json={"symbol": "XAUUSD", "side": "long"})
    assert r.status_code == 422 and "contract_multiplier" in r.json()["error"]["details"]["missing"]
    inst = instrument()
    t = client.post("/v1/trades", headers=m.h, json={"instrument_id": inst["instrument_id"], "side": "short"}).json()
    assert (t["symbol"], t["contract_type"], t["contract_multiplier"], t["settlement_currency"]) == \
        (inst["symbol"], "linear", "1", "USDT")
    assert t["status"] == "draft" and t["funding_status"] == "unknown"
    clash = client.post("/v1/trades", headers=m.h, json={"instrument_id": inst["instrument_id"], "side": "long",
                                                         "contract_multiplier": "100"})
    assert clash.status_code == 422 and clash.json()["error"]["code"] == "spec_mismatch"
    assert client.post("/v1/trades", headers=m.h, json={"symbol": "X", "side": "long", "source": "signal"}).status_code == 422


def test_fill_lifecycle_partial_fills_exits_fees_and_risk_lock(client, member):
    m = member()
    t = _trade(client, m, initial_risk_amount="20", initial_risk_currency="USDT")
    tid = t["id"]
    r = _fill(client, m, tid, 0, "entry", "0.4", "100", fee="0.04")
    assert r.status_code == 201 and r.json()["trade"]["status"] == "open"
    assert r.json()["trade"]["risk_locked_at"] is not None
    _fill(client, m, tid, 1, "entry", "0.6", "110", fee="0.066")
    r = _fill(client, m, tid, 2, "exit", "0.5", "120", fee="0.06")        # TP1
    assert r.json()["trade"]["status"] == "partially_closed"
    locked = client.patch(f"/v1/trades/{tid}", headers=m.h, json={"initial_risk_amount": "5"})
    assert locked.status_code == 409 and locked.json()["error"]["code"] == "initial_risk_locked"
    over = _fill(client, m, tid, 3, "exit", "0.6", "106")
    assert over.status_code == 422 and over.json()["error"]["code"] == "invalid_fill_sequence"
    r = _fill(client, m, tid, 3, "exit", "0.5", "106", fee="0.053")      # rest at average entry (BE)
    s = r.json()["trade"]["summary"]
    assert r.json()["trade"]["status"] == "closed"
    assert s["avg_entry_price"] == "106" and s["gross_pnl"] == "7"       # 0.5 x (120 - 106) + 0
    assert s["fees"] == "0.219" and s["net_pnl"] == "6.781" and s["net_r"] == "0.339"
    assert s["complete"] is True
    detail = client.get(f"/v1/trades/{tid}", headers=m.h).json()
    assert len(detail["fills"]) == 4                                     # the rejected fill was not stored
    cancel = client.patch(f"/v1/trades/{tid}", headers=m.h, json={"status": "cancelled"})
    assert cancel.status_code == 409


def test_import_key_is_idempotent_and_conflicts_on_change(client, member, admin):
    m = member()
    tid = _trade(client, m)["id"]
    a = _fill(client, m, tid, 0, "entry", "1", "100", import_key="binance:trade:42")
    b = _fill(client, m, tid, 0, "entry", "1", "100", import_key="binance:trade:42")
    assert a.status_code == 201 and b.status_code == 200 and a.json()["fill"]["id"] == b.json()["fill"]["id"]
    c = _fill(client, m, tid, 0, "entry", "2", "100", import_key="binance:trade:42")
    assert c.status_code == 409 and c.json()["error"]["code"] == "import_key_conflict"
    assert admin.execute("SELECT count(*) AS n FROM fills WHERE trade_id = %s", [tid]).fetchone()["n"] == 1
    # the key is unique per member, not globally
    other = member()
    otid = _trade(client, other)["id"]
    assert _fill(client, other, otid, 0, "entry", "1", "100", import_key="binance:trade:42").status_code == 201


def test_corrections_keep_the_audit_trail(client, member, admin):
    m = member()
    tid = _trade(client, m)["id"]
    wrong = _fill(client, m, tid, 0, "entry", "1", "1000").json()["fill"]   # typo: should be 100
    r = _fill(client, m, tid, 0, "entry", "1", "100", corrects_fill_id=wrong["id"], correction_reason="พิมพ์ราคาผิด")
    assert r.status_code == 201
    r = _fill(client, m, tid, 5, "exit", "1", "110")
    trade = r.json()["trade"]
    assert trade["summary"]["gross_pnl"] == "10"
    assert [f["superseded"] for f in trade["fills"]] == [True, False, False]
    again = _fill(client, m, tid, 0, "entry", "1", "99", corrects_fill_id=wrong["id"], correction_reason="again")
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_corrected"
    # fills are never updated in place, even by the owner role
    import psycopg
    import pytest
    with pytest.raises(psycopg.errors.RestrictViolation):
        admin.execute("UPDATE fills SET price = 1 WHERE id = %s", [wrong["id"]])


def test_cashflows_inverse_contract_and_funding(client, member):
    m = member()
    t = _trade(client, m, symbol="BTCUSD_PERP", contract_type="inverse", contract_multiplier="100",
               quote_currency="USD", settlement_currency="BTC", side="short", funding_status="unknown",
               initial_risk_amount="0.001", initial_risk_currency="BTC")
    tid = t["id"]
    _fill(client, m, tid, 0, "entry", "10", "50000", fee="0.00001", fee_currency="BTC")
    _fill(client, m, tid, 60, "exit", "10", "40000", fee="0.00001", fee_currency="BTC")
    r = client.post(f"/v1/trades/{tid}/cashflows", headers=m.h, json={
        "ts": (T0 + timedelta(minutes=30)).isoformat(), "kind": "funding", "amount": "-0.00002", "currency": "BTC",
        "import_key": "funding:1"})
    assert r.status_code == 201
    dup = client.post(f"/v1/trades/{tid}/cashflows", headers=m.h, json={
        "ts": (T0 + timedelta(minutes=30)).isoformat(), "kind": "funding", "amount": "-0.00002", "currency": "BTC",
        "import_key": "funding:1"})
    assert dup.status_code == 200
    s = dup.json()["trade"]["summary"]
    # short inverse: 10 x 100 x (1/40000 - 1/50000) = 0.005 BTC
    assert s["gross_pnl"] == "0.005" and s["funding"] == "-0.00002" and s["net_pnl"] == "0.00496"
    assert s["complete"] is False and s["incomplete_reasons"] == ["funding_unknown"]
    client.patch(f"/v1/trades/{tid}", headers=m.h, json={"funding_status": "recorded"})
    s = client.get(f"/v1/trades/{tid}", headers=m.h).json()["summary"]
    assert s["complete"] is True and s["net_r"] == "4.96"


def test_net_r_not_available_without_initial_risk(client, member):
    m = member()
    tid = _trade(client, m)["id"]
    _fill(client, m, tid, 0, "entry", "1", "100")
    s = _fill(client, m, tid, 1, "exit", "1", "101").json()["trade"]["summary"]
    assert s["net_r"] is None and s["r_unavailable_reason"] == "no_initial_risk"


def test_stats_cover_the_whole_range_not_a_page(client, member, admin):
    m = member()
    n = 520
    with admin.cursor() as cur:
        trades, fills = [], []
        for i in range(n):
            tid = uuid.uuid4()
            closed = T0 + timedelta(hours=i)
            win = i % 4 != 0                    # 390 winners, 130 losers
            exit_price = Decimal("102") if win else Decimal("99")
            trades.append((tid, m.id, i))
            fills.append((uuid.uuid4(), m.id, tid, closed - timedelta(minutes=30), "entry", Decimal("1"), Decimal("100")))
            fills.append((uuid.uuid4(), m.id, tid, closed, "exit", Decimal("1"), exit_price))
        cur.executemany(
            "INSERT INTO personal_trades (id, owner_id, venue_label, symbol, market_type, contract_type,"
            " contract_multiplier, quote_currency, settlement_currency, side, initial_risk_amount, initial_risk_currency,"
            " source, status, funding_status, opened_at, closed_at)"
            " VALUES (%s, %s, 'binance', 'BTCUSDT', 'perpetual', 'linear', 1, 'USDT', 'USDT', 'long', 2, 'USDT',"
            " 'import', 'closed', 'recorded', %s, %s)",
            [(tid, owner, T0 + timedelta(hours=i) - timedelta(minutes=30), T0 + timedelta(hours=i))
             for tid, owner, i in trades])
        cur.executemany("INSERT INTO fills (id, owner_id, trade_id, ts, action, quantity, price, fee, fee_currency)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, 0, 'USDT')", fills)
    stats = client.get("/v1/trades/stats", headers=m.h).json()
    assert stats["counts"]["closed"] == n and stats["counts"]["complete"] == n
    assert (stats["counts"]["wins"], stats["counts"]["losses"]) == (390, 130)
    assert stats["win_rate"] == "0.75"
    usdt = stats["by_currency"]["USDT"]
    assert usdt["net_pnl"] == str(390 * 2 - 130 * 1)                  # 650
    assert usdt["profit_factor"] == "6"                               # 780 / 130
    assert stats["r"]["mean"] == "0.625" and stats["r"]["sample"] == n
    assert usdt["realized_equity_max_drawdown"]["amount"] == "1"
    assert "mark-to-market" in stats["definitions"]["realized_equity_max_drawdown"]
    # range filter on close time
    rng = client.get("/v1/trades/stats", headers=m.h, params={
        "from": (T0 + timedelta(hours=100)).isoformat(), "to": (T0 + timedelta(hours=200)).isoformat()}).json()
    assert rng["counts"]["closed"] == 100
    # listing pages at most 100 at a time; the stats did not depend on it
    page = client.get("/v1/trades", headers=m.h, params={"limit": 100}).json()
    assert len(page["items"]) == 100 and page["next_cursor"]
