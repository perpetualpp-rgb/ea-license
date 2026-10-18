"""Delayed members never receive market data newer than now - 15 minutes, on any market
endpoint, and aggregates are computed only from rows older than the cut-off."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from conftest import now_utc, seed_market, ts

DELAY = timedelta(minutes=15)
SLACK = timedelta(seconds=5)   # test runtime between the request and our "now"


def _gap(admin, iid, key, start, end=None, status="open", repaired=None, stream="trades"):
    admin.execute("INSERT INTO data_quality (instrument_id, stream, source_key, reason, status, gap_start, gap_end,"
                  " repaired_at) VALUES (%s, %s, %s, 'test', %s, %s, %s, %s)",
                  [iid, stream, key, status, start, end, repaired])


def test_bars_never_include_rows_newer_than_cutoff(client, member, admin, instrument):
    inst = instrument()
    iid = inst["instrument_id"]
    seed_market(admin, inst, minutes=120)
    delayed, realtime = member(), member(realtime=True, admin_conn=admin)
    for tf in ("1m", "5m", "1h"):
        for source in ("kline", "trades"):
            r = client.get(f"/v1/instruments/{iid}/bars", headers=delayed.h,
                           params={"tf": tf, "source": source, "from": (now_utc() - timedelta(hours=3)).isoformat(),
                                   "to": (now_utc() + timedelta(hours=1)).isoformat()})
            assert r.status_code == 200, r.text
            body = r.json()
            cutoff = ts(body["access"]["data_until"])
            assert body["access"]["level"] == "delayed" and body["access"]["delay_minutes"] == 15
            assert now_utc() - DELAY - SLACK <= cutoff <= now_utc() - DELAY
            assert ts(body["to"]) == cutoff
            assert body["bars"] and all(ts(b["t"]) < cutoff for b in body["bars"])
            # the aggregates contain exactly the rows older than the cut-off, nothing newer
            if source == "kline":
                expect = admin.execute("SELECT sum(volume) AS v FROM klines WHERE instrument_id = %s AND"
                                       " open_time >= %s AND open_time + interval '60 seconds' <= %s",
                                       [iid, ts(body["from"]), cutoff]).fetchone()["v"]
            else:
                expect = admin.execute("SELECT sum(qty) AS v FROM trades WHERE instrument_id = %s AND event_ts >= %s"
                                       " AND event_ts < %s", [iid, ts(body["from"]), cutoff]).fetchone()["v"]
            assert sum(Decimal(b["volume"]) for b in body["bars"]) == expect
    # a realtime member sees the newest minutes
    rt = client.get(f"/v1/instruments/{iid}/bars", headers=realtime.h, params={"tf": "1m"}).json()
    assert rt["access"]["level"] == "realtime"
    assert ts(rt["bars"][-1]["t"]) > now_utc() - timedelta(minutes=3)


def test_trade_bars_keep_unknown_side_out_of_delta(client, member, admin, instrument):
    inst = instrument()
    seed_market(admin, inst, minutes=60)
    m = member(realtime=True, admin_conn=admin)
    bars = client.get(f"/v1/instruments/{inst['instrument_id']}/bars", headers=m.h,
                      params={"tf": "1m", "source": "trades"}).json()["bars"]
    for b in bars:
        buy, sell, unknown = (Decimal(b[k]) for k in ("taker_buy_volume", "taker_sell_volume", "unknown_side_volume"))
        assert Decimal(b["delta"]) == buy - sell
        assert buy + sell + unknown == Decimal(b["volume"])
    assert any(Decimal(b["unknown_side_volume"]) > 0 for b in bars)


def test_quality_endpoint_is_delayed_too(client, member, admin, instrument):
    inst = instrument()
    iid = inst["instrument_id"]
    seed_market(admin, inst, minutes=90)
    now = now_utc()
    _gap(admin, iid, "recent", now - timedelta(minutes=5))                                    # newer than cut-off
    _gap(admin, iid, "old-repaired-late", now - timedelta(minutes=40), now - timedelta(minutes=10),
         status="recovered", repaired=now - timedelta(minutes=2))
    _gap(admin, iid, "old-closed", now - timedelta(minutes=60), now - timedelta(minutes=50), status="unrecoverable",
         stream="book")
    delayed = member()
    body = client.get(f"/v1/instruments/{iid}/quality", headers=delayed.h,
                      params={"from": (now - timedelta(hours=2)).isoformat(), "to": now.isoformat()}).json()
    cutoff = ts(body["access"]["data_until"])
    assert ts(body["to"]) == cutoff
    keys = {g["reason"]: g for g in body["gaps"]}
    assert len(body["gaps"]) == 2                    # the gap that started 5 minutes ago is not revealed
    for g in body["gaps"]:
        for k in ("gap_start", "gap_end", "repaired_at", "detected_at"):
            assert g[k] is None or ts(g[k]) < cutoff, (k, g)
    late = next(g for g in body["gaps"] if g["stream"] == "trades")
    assert late["status"] == "open" and late["gap_end"] is None and late["repaired_at"] is None  # as of the cut-off
    assert keys and all(ts(s["last"]) < cutoff for n, s in body["streams"].items() if n != "trades" and s["last"])
    # realtime sees all three, with the repair
    rt = member(realtime=True, admin_conn=admin)
    rbody = client.get(f"/v1/instruments/{iid}/quality", headers=rt.h,
                       params={"from": (now - timedelta(hours=2)).isoformat()}).json()
    assert len(rbody["gaps"]) == 3
    assert {g["status"] for g in rbody["gaps"]} == {"open", "recovered", "unrecoverable"}


def test_coverage_of_a_day_without_data_is_not_100(client, member, admin, instrument):
    inst = instrument()
    m = member(realtime=True, admin_conn=admin)
    end = now_utc() - timedelta(days=3)
    body = client.get(f"/v1/instruments/{inst['instrument_id']}/quality", headers=m.h,
                      params={"from": (end - timedelta(days=1)).isoformat(), "to": end.isoformat()}).json()
    for name in ("book", "kline"):
        assert body["streams"][name]["has_data"] is False
        assert body["streams"][name]["coverage_pct"] == "0"
    assert body["streams"]["trades"]["coverage_pct"] is None   # no ids seen: unknown, not 100%
    # collector stopped for the second half of a window: coverage shows it
    seed_market(admin, inst, minutes=60, end=now_utc() - timedelta(hours=1))
    half = client.get(f"/v1/instruments/{inst['instrument_id']}/quality", headers=m.h,
                      params={"from": (now_utc() - timedelta(hours=2)).isoformat(),
                              "to": now_utc().isoformat()}).json()
    assert 40 <= float(half["streams"]["kline"]["coverage_pct"]) <= 55
    # book rows were seeded every 10 s, so half a window holds about 5% of its seconds
    assert 4 <= float(half["streams"]["book"]["coverage_pct"]) <= 5.5


def test_instrument_list_and_metric_events_are_delayed(client, member, admin, instrument):
    inst = instrument()
    iid = inst["instrument_id"]
    seed_market(admin, inst, minutes=40)
    now = now_utc()
    for minutes in (1, 30):
        admin.execute("INSERT INTO metric_events (instrument_id, metric_type, window_start, window_end, source_watermark,"
                      " unit, value, parameters_hash, engine_version) VALUES (%s, 'cvd_5m', %s, %s, %s, 'BTC', 12.5, 'h', 'e1')",
                      [iid, now - timedelta(minutes=minutes + 5), now - timedelta(minutes=minutes),
                       now - timedelta(minutes=minutes)])
    delayed = member()
    lst = client.get("/v1/instruments", headers=delayed.h).json()
    cutoff = ts(lst["access"]["data_until"])
    mine = next(i for i in lst["items"] if i["instrument_id"] == iid)
    for k in ("latest_trade_at", "latest_kline_close_at", "latest_book_at"):
        assert ts(mine[k]) <= cutoff
    ev = client.get(f"/v1/instruments/{iid}/metric-events", headers=delayed.h).json()["items"]
    assert len(ev) == 1 and ts(ev[0]["window_end"]) < cutoff and ev[0]["value"] == "12.5"
    rt = member(realtime=True, admin_conn=admin)
    assert len(client.get(f"/v1/instruments/{iid}/metric-events", headers=rt.h).json()["items"]) == 2
    rt_inst = next(i for i in client.get("/v1/instruments", headers=rt.h).json()["items"] if i["instrument_id"] == iid)
    assert ts(rt_inst["latest_trade_at"]) > cutoff


def test_market_endpoints_reject_bad_requests(client, member, instrument):
    m = member()
    iid = instrument()["instrument_id"]
    assert client.get("/v1/instruments/999999999", headers=m.h).status_code == 404
    assert client.get(f"/v1/instruments/{iid}/bars", headers=m.h, params={"tf": "7m"}).status_code == 422
    assert client.get(f"/v1/instruments/{iid}/bars", headers=m.h,
                      params={"from": "2026-10-01T00:00:00"}).status_code == 422   # no timezone
    far = client.get(f"/v1/instruments/{iid}/bars", headers=m.h,
                     params={"tf": "1m", "from": "2020-01-01T00:00:00Z"})
    assert far.status_code == 422 and far.json()["error"]["code"] == "range_too_large"
