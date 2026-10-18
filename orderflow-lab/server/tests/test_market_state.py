"""/quote, /details and /observations: entitlement cut-off, engine results on synthetic rows,
and null (never 0) when an input is missing. All market rows here are generated, not real data."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from conftest import now_utc, seed_futures_state, seed_market, seed_wall_book, ts

from app import orderflow
from app.common import dstr
from engine import bigtrades as eng_big
from engine import profile as eng_profile
from feeds.types import Trade

UTC = timezone.utc
DELAY = timedelta(minutes=15)


def _trades(admin, inst, rows, start_id=1):
    """rows: (event_ts, price, qty, side)"""
    with admin.cursor() as cur:
        cur.executemany(
            "INSERT INTO trades (venue, market_type, symbol, agg_trade_id, instrument_id, event_ts, price, qty, taker_side)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [(inst["venue"], inst["market_type"], inst["symbol"], start_id + k, inst["instrument_id"], t, p, q, s)
             for k, (t, p, q, s) in enumerate(rows)])


def _kline(admin, iid, open_time, close):
    admin.execute("INSERT INTO klines (instrument_id, interval_s, open_time, open, high, low, close, volume,"
                  " trade_count, taker_buy_volume) VALUES (%s, 60, %s, %s, %s, %s, %s, 1, 1, 0.5)",
                  [iid, open_time, close, close, close, close])


def _gap(admin, iid, stream, key, reason, start, end=None, status="open"):
    admin.execute("INSERT INTO data_quality (instrument_id, stream, source_key, reason, status, gap_start, gap_end)"
                  " VALUES (%s, %s, %s, %s, %s, %s, %s)", [iid, stream, key, reason, status, start, end])


def _get(client, m, iid, what, **params):
    r = client.get(f"/v1/instruments/{iid}/{what}", headers=m.h, params=params)
    assert r.status_code == 200, r.text
    return r.json()


# -- quote ---------------------------------------------------------------------------------------
def test_quote_change_24h_needs_a_kline_24h_back(client, member, admin, instrument):
    inst = instrument()
    iid = inst["instrument_id"]
    seed_market(admin, inst, minutes=90)
    m = member(realtime=True, admin_conn=admin)
    q = _get(client, m, iid, "quote")
    newest = admin.execute("SELECT price, event_ts FROM trades WHERE instrument_id = %s ORDER BY event_ts DESC LIMIT 1",
                           [iid]).fetchone()
    assert q["last_trade"] == {"price": dstr(newest["price"]), "at": q["last_trade"]["at"], "source": "trade"}
    assert ts(q["last_trade"]["at"]) == newest["event_ts"]
    assert q["change_24h"] is None                     # only 90 minutes stored: never a shorter window
    assert q["mark"] is None                           # no mark rows collected
    assert 1 <= len(q["spark"]) <= 49
    spark_t = [ts(p["t"]) for p in q["spark"]]
    assert spark_t == sorted(spark_t) and spark_t[-1] <= ts(q["last_trade"]["at"])
    assert all(spark_t[-1] - t >= timedelta(minutes=29) * (len(spark_t) - 1 - k) for k, t in enumerate(spark_t))

    at = ts(q["last_trade"]["at"])
    _kline(admin, iid, at - timedelta(hours=24, minutes=12), Decimal("10"))   # close 11 min off: outside +-5 min
    assert _get(client, m, iid, "quote")["change_24h"] is None
    ref_close = at - timedelta(hours=24, minutes=2)
    _kline(admin, iid, ref_close - timedelta(minutes=1), Decimal("80"))
    q = _get(client, m, iid, "quote")
    price = Decimal(q["last_trade"]["price"])
    expect = ((price / Decimal(80) - 1) * 100).quantize(Decimal("0.01"))
    assert q["change_24h"] == {"pct": format(expect, "f"), "ref_price": "80", "ref_at": q["change_24h"]["ref_at"]}
    assert ts(q["change_24h"]["ref_at"]) == ref_close
    assert ts(q["spark"][0]["t"]) <= at - timedelta(hours=23, minutes=30)   # the old kline is on the sparkline too


def test_quote_falls_back_to_kline_and_reports_mark(client, member, admin, instrument):
    inst = instrument()
    iid = inst["instrument_id"]
    end = now_utc().replace(second=0, microsecond=0) - timedelta(hours=1)
    _kline(admin, iid, end - timedelta(minutes=1), Decimal("4012.5"))
    seed_futures_state(admin, inst, end - timedelta(minutes=10), end)
    m = member(realtime=True, admin_conn=admin)
    q = _get(client, m, iid, "quote")
    assert q["last_trade"] == {"price": "4012.5", "at": q["last_trade"]["at"], "source": "kline"}
    assert ts(q["last_trade"]["at"]) == end
    mark = admin.execute("SELECT * FROM mark_prices WHERE instrument_id = %s ORDER BY event_ts DESC LIMIT 1",
                         [iid]).fetchone()
    assert q["mark"] == {"price": format(mark["mark_price"].normalize(), "f"),
                         "index": format(mark["index_price"].normalize(), "f"), "at": q["mark"]["at"]}
    assert ts(q["mark"]["at"]) == mark["event_ts"]
    e = _get(client, m, instrument()["instrument_id"], "quote")
    assert (e["last_trade"], e["mark"], e["change_24h"], e["spark"]) == (None, None, None, [])


# -- entitlement cut-off on all three endpoints ------------------------------------------------------
def _times(obj, skip=("next_at",)):
    """Every ISO time in a response body (except scheduled times such as the next funding)."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in skip or k == "access":
                continue
            if isinstance(v, str) and k in ("at", "t", "last_at", "ref_at", "from", "to"):
                out.append((k, ts(v)))
            else:
                out += _times(v, skip)
    elif isinstance(obj, list):
        for v in obj:
            out += _times(v, skip)
    return out


def test_delayed_member_never_sees_rows_newer_than_cutoff(client, member, admin, instrument):
    inst = instrument()
    iid = inst["instrument_id"]
    now = now_utc()
    seed_market(admin, inst, minutes=60, end=now, book=False)
    seed_futures_state(admin, inst, now - timedelta(minutes=70), now + timedelta(seconds=1))
    seed_wall_book(admin, inst, now - timedelta(minutes=30), minutes=30, every_s=2, hole=None)
    admin.execute("INSERT INTO liquidations (instrument_id, event_ts, side, price, avg_price, qty, status) VALUES"
                  " (%s, %s, 'sell', 100, 100, 1, 'FILLED'), (%s, %s, 'buy', 101, 101, 2, 'FILLED')",
                  [iid, now - timedelta(minutes=20), iid, now - timedelta(minutes=1)])
    # a day of history (as the collector's REST kline_history now gives a fresh start): one 1m kline 24 h
    # before the delayed member's last trade, one 24 h before the realtime member's
    for close_at, close in ((now - DELAY - timedelta(hours=24), "90"), (now - timedelta(hours=24), "95")):
        _kline(admin, iid, close_at.replace(second=0, microsecond=0) - timedelta(minutes=1), Decimal(close))
    delayed = member()
    q = _get(client, delayed, iid, "quote")
    cutoff = ts(q["access"]["data_until"])
    # change_24h is computed from the delayed last trade and a kline closed 24 h before it, nothing newer
    assert q["change_24h"]["ref_price"] == "90" and ts(q["change_24h"]["ref_at"]) < cutoff - timedelta(hours=23)
    assert q["change_24h"]["pct"] == format(((Decimal(q["last_trade"]["price"]) / 90 - 1) * 100).quantize(
        Decimal("0.01")), "f")
    assert q["access"]["level"] == "delayed" and now - DELAY - timedelta(seconds=5) <= cutoff <= now_utc() - DELAY
    want_trade = admin.execute("SELECT event_ts FROM trades WHERE instrument_id = %s AND event_ts < %s"
                               " ORDER BY event_ts DESC LIMIT 1", [iid, cutoff]).fetchone()["event_ts"]
    want_mark = admin.execute("SELECT event_ts FROM mark_prices WHERE instrument_id = %s AND event_ts < %s"
                              " ORDER BY event_ts DESC LIMIT 1", [iid, cutoff]).fetchone()["event_ts"]
    assert ts(q["last_trade"]["at"]) == want_trade and ts(q["mark"]["at"]) == want_mark
    assert all(t <= cutoff for _, t in _times(q))

    d = _get(client, delayed, iid, "details", to=(now + timedelta(hours=1)).isoformat())
    assert ts(d["to"]) == ts(d["access"]["data_until"]) and ts(d["to"]) - cutoff < timedelta(seconds=5)
    cutoff = ts(d["to"])
    bad = [(k, t) for k, t in _times(d) if t > cutoff]
    assert not bad, bad
    want_oi = admin.execute("SELECT event_ts, open_interest FROM open_interest WHERE instrument_id = %s AND"
                            " event_ts < %s ORDER BY event_ts DESC LIMIT 1", [iid, cutoff]).fetchone()
    assert ts(d["oi"]["at"]) == want_oi["event_ts"] and Decimal(d["oi"]["value"]) == want_oi["open_interest"]
    assert d["book"] is not None and ts(d["book"]["at"]) < cutoff
    assert d["book"]["walls"] and d["funding"] is not None
    liq = next(s for s in d["quality"] if s["stream"] == "liquidations")
    assert ts(liq["last_at"]) == now - timedelta(minutes=20)      # the newer one is not revealed

    o = _get(client, delayed, iid, "observations")
    assert ts(o["at"]) - cutoff < timedelta(seconds=5) and all(t <= ts(o["at"]) for _, t in _times(o))
    assert {i["kind"] for i in o["items"]} >= {"wall", "funding", "oi"}

    # a realtime member sees the newer rows
    rt = member(realtime=True, admin_conn=admin)
    rq = _get(client, rt, iid, "quote")
    assert ts(rq["last_trade"]["at"]) > cutoff and ts(rq["mark"]["at"]) > cutoff
    assert rq["change_24h"]["ref_price"] == "95"
    rd = _get(client, rt, iid, "details")
    assert ts(rd["oi"]["at"]) > cutoff and ts(rd["book"]["at"]) > cutoff


# -- walls -------------------------------------------------------------------------------------
def test_walls_and_event_outcomes_from_the_engine(client, member, admin, instrument):
    inst = instrument()
    iid = inst["instrument_id"]
    t0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
    end = seed_wall_book(admin, inst, t0)
    m = member(realtime=True, admin_conn=admin)
    d = _get(client, m, iid, "details", **{"from": (end - timedelta(hours=1)).isoformat(), "to": end.isoformat()})
    book = d["book"]
    assert book["synced"] is True and ts(book["at"]) == end - timedelta(seconds=2)
    walls = {(w["side"], w["price"]): w for w in book["walls"]}
    assert set(walls) == {("bid", "99.5"), ("ask", "100.6")}
    a, e = walls[("bid", "99.5")], walls[("ask", "100.6")]
    assert (a["status"], a["size"], a["mult"]) == ("holding", "20", "20.0")
    assert 26 * 60 <= a["age_s"] <= 27 * 60 + 30               # re-detected after the 60 s hole, capped by the look-back
    assert (e["status"], e["size"], e["mult"]) == ("pulling", "4.5", "4.5")
    outcomes = {(ev["side"], ev["price"]): [] for ev in book["events"]}
    for ev in book["events"]:
        outcomes[(ev["side"], ev["price"])].append(ev["outcome"])
    assert outcomes[("ask", "100.8")] == ["removed", "unknown"]          # newest first
    assert outcomes[("bid", "99.8")] == ["price_reached", "unknown"]
    assert outcomes[("bid", "99.5")] == ["unknown"] and outcomes[("ask", "100.6")] == ["unknown"]
    assert {ev["outcome"] for ev in book["events"]} <= {"price_reached", "removed", "unknown"}
    at = [ts(ev["at"]) for ev in book["events"]]
    assert at == sorted(at, reverse=True)
    removed = next(ev for ev in book["events"] if ev["outcome"] == "removed")
    assert t0 + timedelta(minutes=10) < ts(removed["at"]) < t0 + timedelta(minutes=11)
    assert "0.1" in book["method"] and "อนุมาน" in book["method"]          # bucket inferred from the levels

    # the newest snapshot more than 60 s before `to`: no walls, tracked walls end as unknown
    late = _get(client, m, iid, "details", **{"from": (end - timedelta(hours=1)).isoformat(),
                                               "to": (end + timedelta(minutes=5)).isoformat()})
    assert late["book"]["walls"] == [] and ts(late["book"]["at"]) == end - timedelta(seconds=2)
    assert [ev["outcome"] for ev in late["book"]["events"][:2]] == ["unknown", "unknown"]
    # an open resync gap at `to` makes the book unusable now
    _gap(admin, iid, "book", "resync:x", "book_resync", end - timedelta(seconds=1))
    gap = _get(client, m, iid, "details", **{"from": (end - timedelta(hours=1)).isoformat(), "to": end.isoformat()})
    assert gap["book"]["synced"] is False and gap["book"]["walls"] == []
    assert next(s for s in gap["quality"] if s["stream"] == "book")["status"] == "book_resync"
    # no snapshot at all -> book is null
    other = instrument()
    assert _get(client, m, other["instrument_id"], "details", to=end.isoformat())["book"] is None


# -- big trades --------------------------------------------------------------------------------
def test_big_trade_threshold_has_no_look_ahead(client, member, admin, instrument):
    inst = instrument()
    iid = inst["instrument_id"]
    t = datetime(2026, 3, 3, 12, 0, tzinfo=UTC)
    pre = [(t - timedelta(hours=4) + timedelta(seconds=20 * k), Decimal("100"), Decimal(k + 1) / 100,
            "buy" if k % 2 else "sell") for k in range(600)]                    # sizes 0.01 .. 6.00
    window = ([(t + timedelta(seconds=30 * k), Decimal("101"), Decimal(1), "buy") for k in range(50)]
              + [(t + timedelta(minutes=30, seconds=k), Decimal("102"), Decimal(7), "sell" if k % 2 else "buy")
                 for k in range(10)]
              + [(t + timedelta(minutes=40, seconds=k), Decimal("103"), Decimal(100), "buy") for k in range(3)]
              + [(t + timedelta(minutes=41, seconds=k), Decimal("103"), Decimal(9), "unknown") for k in range(2)])
    _trades(admin, inst, pre + window)
    m = member(realtime=True, admin_conn=admin)
    d = _get(client, m, iid, "details", **{"from": t.isoformat(), "to": (t + timedelta(hours=1)).isoformat()})
    big = d["big_trades"]
    engine_thr = eng_big.threshold([Trade(0, 100.0, float(q), s, "x", "x", "") for _, _, q, s in pre], 0.99)
    assert big["threshold"] == "5.94" and engine_thr == 5.94      # P99 of the 4 h before `from`, same as the engine
    assert len(big["items"]) == 13                                   # 10 x 7 + 3 x 100; the window's own P99 would be 100
    assert {i["side"] for i in big["items"]} <= {"buy", "sell"}      # the two unknown-side 9s are left out
    at = [ts(i["at"]) for i in big["items"]]
    assert at == sorted(at, reverse=True) and all(t <= x < t + timedelta(hours=1) for x in at)
    assert "4 ชั่วโมงก่อนช่วงที่แสดง" in big["method"] and "ไม่รวม 2 ไม้ที่ไม่ทราบฝั่ง taker" in big["method"]

    # fewer than 500 trades before `from`: the window itself is used and the method says so
    thin = instrument()
    _trades(admin, thin, pre[-100:] + window)
    d2 = _get(client, m, thin["instrument_id"], "details",
              **{"from": t.isoformat(), "to": (t + timedelta(hours=1)).isoformat()})
    assert d2["big_trades"]["threshold"] == "100" and len(d2["big_trades"]["items"]) == 3
    assert "look-ahead" in d2["big_trades"]["method"] and "100 รายการ" in d2["big_trades"]["method"]
    # no trades anywhere: null, not 0
    none = _get(client, m, instrument()["instrument_id"], "details", to=t.isoformat())
    assert none["big_trades"]["threshold"] is None and none["big_trades"]["items"] == []


def test_sql_percentile_matches_engine_threshold(admin, instrument, dsns):
    rnd = random.Random(7)
    for n in (500, 517, 777, 1000, 1234):
        inst = instrument()
        t = datetime(2026, 3, 4, 0, 0, tzinfo=UTC)
        sizes = [Decimal(rnd.randint(1, 50000)) / 1000 for _ in range(n)]
        _trades(admin, inst, [(t + timedelta(seconds=k), Decimal(100), q, "buy") for k, q in enumerate(sizes)])
        thr, basis, used, _ = orderflow.big_threshold(admin, inst["instrument_id"], t + timedelta(hours=4),
                                                      t + timedelta(hours=5))
        assert basis == "pre_window" and used == n
        assert thr == Decimal(repr(eng_big.threshold([Trade(0, 1.0, float(q), "buy", "", "", "") for q in sizes]))), n


# -- profile -----------------------------------------------------------------------------------
def test_profile_from_trades_matches_engine_and_falls_back_to_klines(client, member, admin, instrument):
    assert orderflow.profile_bin(Decimal("81700.03"), None) == Decimal(10)
    assert orderflow.profile_bin(Decimal("4012.35"), None) == Decimal("0.5")
    assert orderflow.profile_bin(Decimal("3990"), None) == Decimal("0.5")
    assert orderflow.profile_bin(Decimal("100.3"), None) == Decimal("0.02")
    assert orderflow.profile_bin(Decimal("4012.35"), Decimal("0.3")) == Decimal("0.6")      # whole ticks
    assert orderflow.profile_bin(Decimal("100.3"), Decimal("0.1")) == Decimal("0.1")        # at least one tick
    inst = instrument()
    iid = inst["instrument_id"]
    t = datetime(2026, 3, 5, 8, 0, tzinfo=UTC)
    rnd = random.Random(3)
    rows = [(t + timedelta(seconds=7 * k), Decimal("100") + Decimal(rnd.randint(0, 80)) / 100,
             Decimal(rnd.randint(1, 900)) / 1000, ("buy", "sell", "unknown")[k % 3]) for k in range(400)]
    _trades(admin, inst, rows)
    m = member(realtime=True, admin_conn=admin)
    p = _get(client, m, iid, "details", **{"from": t.isoformat(), "to": (t + timedelta(hours=1)).isoformat()})["profile"]
    engine = eng_profile.profile_from_trades(
        [Trade(0, float(pr), float(q), s, "", "", "") for _, pr, q, s in rows], 0.02)
    va = eng_profile.value_area(engine, 0.70)
    assert p["bin_size"] == "0.02"
    assert (Decimal(p["poc"]), Decimal(p["va_low"]), Decimal(p["va_high"])) == \
        tuple(Decimal(repr(x)).quantize(Decimal("0.01")) for x in (eng_profile.poc(engine), *va))
    assert "จากเทรดทุกรายการ" in p["method"] and "5,000" in p["method"]
    # klines only: approximation, stated
    k_only = instrument()
    for i in range(30):
        _kline(admin, k_only["instrument_id"], t + timedelta(minutes=i), Decimal("4000") + i)
    pk = _get(client, m, k_only["instrument_id"], "details",
              **{"from": t.isoformat(), "to": (t + timedelta(hours=1)).isoformat()})["profile"]
    assert pk["bin_size"] == "0.5" and "ค่าประมาณ" in pk["method"] and "แท่ง 1 นาที" in pk["method"]
    assert pk["poc"] is not None and Decimal(pk["va_low"]) <= Decimal(pk["poc"]) <= Decimal(pk["va_high"])
    assert _get(client, m, instrument()["instrument_id"], "details", to=t.isoformat())["profile"] is None


# -- open interest / funding / spot ---------------------------------------------------------------
def test_oi_change_is_null_without_a_row_at_that_distance(client, member, admin, instrument):
    inst = instrument()
    iid = inst["instrument_id"]
    end = datetime(2026, 3, 6, 10, 0, tzinfo=UTC)
    seed_futures_state(admin, inst, end - timedelta(minutes=3), end)          # 3 minutes of OI only
    m = member(realtime=True, admin_conn=admin)
    rng = {"from": (end - timedelta(hours=2)).isoformat(), "to": end.isoformat()}
    oi = _get(client, m, iid, "details", **rng)["oi"]
    assert oi["value"] == "1004" and ts(oi["at"]) == end - timedelta(minutes=1)
    assert oi["change_5m_pct"] is None and oi["change_1h_pct"] is None
    assert oi["unit"] == inst["base_asset"] and [p["value"] for p in oi["series"]] == ["1000", "1002", "1004"]

    full = instrument()
    seed_futures_state(admin, full, end - timedelta(minutes=70), end)       # OI 1000 + 2i every minute
    oi = _get(client, m, full["instrument_id"], "details", **rng)["oi"]
    assert oi["value"] == "1138"                                             # i = 69
    assert oi["change_5m_pct"] == format(((Decimal(1138) / 1128 - 1) * 100).quantize(Decimal("0.01")), "f")
    assert oi["change_1h_pct"] == format(((Decimal(1138) / 1018 - 1) * 100).quantize(Decimal("0.01")), "f")
    # a hole around 5 minutes back (more than one polling interval wide): no 5-minute change
    admin.execute("DELETE FROM open_interest WHERE instrument_id = %s AND event_ts BETWEEN %s AND %s",
                  [full["instrument_id"], end - timedelta(minutes=7), end - timedelta(minutes=5)])
    oi = _get(client, m, full["instrument_id"], "details", **rng)["oi"]
    assert oi["change_5m_pct"] is None and oi["change_1h_pct"] is not None
    f = _get(client, m, full["instrument_id"], "details", **rng)["funding"]
    assert f["rate_pct"] == "0.0100" and f["kind"] == "estimated" and ts(f["next_at"]) == datetime(2026, 3, 6, 16, tzinfo=UTC)
    prices = {p["type"]: p for p in _get(client, m, full["instrument_id"], "details", **rng)["prices"]}
    assert prices["mark"]["value"] is not None and prices["index"]["value"] is not None
    assert prices["last_trade"] == {"type": "last_trade", "value": None, "at": None}   # no trades stored


def test_spot_has_no_oi_funding_or_mark(client, member, admin, instrument):
    inst = instrument(market_type="spot")
    iid = inst["instrument_id"]
    end = datetime(2026, 3, 7, 10, 0, tzinfo=UTC)
    seed_market(admin, inst, minutes=60, end=end)
    seed_futures_state(admin, inst, end - timedelta(minutes=60), end)   # even if such rows existed, spot ignores them
    m = member(realtime=True, admin_conn=admin)
    d = _get(client, m, iid, "details", **{"from": (end - timedelta(hours=1)).isoformat(), "to": end.isoformat()})
    assert d["oi"] is None and d["funding"] is None
    prices = {p["type"]: p for p in d["prices"]}
    assert prices["mark"] == {"type": "mark", "value": None, "at": None} and prices["index"]["value"] is None
    assert prices["last_trade"]["value"] is not None
    q = {s["stream"]: s for s in d["quality"]}
    for s in ("oi", "mark", "liquidations"):
        assert q[s]["status"] == "unavailable" and "spot" in q[s]["note"] and "funding" in q[s]["note"]
    assert q["trades"]["status"] == "ok" and q["kline"]["status"] == "ok"
    assert _get(client, m, iid, "quote")["mark"] is None
    kinds = {i["kind"] for i in _get(client, m, iid, "observations", to=end.isoformat())["items"]}
    assert not kinds & {"oi", "funding"}


# -- quality -------------------------------------------------------------------------------------
def test_quality_statuses(client, member, admin, instrument):
    inst = instrument()
    iid = inst["instrument_id"]
    end = datetime(2026, 3, 8, 10, 0, tzinfo=UTC)
    seed_market(admin, inst, minutes=60, end=end, book=False)
    seed_futures_state(admin, inst, end - timedelta(minutes=60), end)
    seed_wall_book(admin, inst, end - timedelta(minutes=5), minutes=5, every_s=1, hole=None)
    m = member(realtime=True, admin_conn=admin)

    def q(to=end):
        body = _get(client, m, iid, "details", to=to.isoformat())
        return {s["stream"]: s for s in body["quality"]}

    ok = q()
    assert [s for s in ok] == ["trades", "book", "kline", "oi", "mark", "liquidations"]
    assert {k: v["status"] for k, v in ok.items()} == {k: "ok" for k in ok}
    assert ok["trades"]["note"] is None and ok["liquidations"]["last_at"] is None
    assert "ไม่ได้แปลว่าข้อมูลขาด" in ok["liquidations"]["note"]
    stale = q(end + timedelta(minutes=10))
    assert {k: v["status"] for k, v in stale.items()} == {k: "stale" for k in stale}
    _gap(admin, iid, "trades", "idgap:1-2", "id_gap_detected", end - timedelta(minutes=3), end - timedelta(minutes=2),
         status="partial")
    assert q()["trades"]["status"] == "incomplete" and q()["book"]["status"] == "ok"
    _gap(admin, iid, "kline", "disconnect:1", "disconnected", end - timedelta(seconds=30))
    down = q()
    assert {k: v["status"] for k, v in down.items()} == {
        "trades": "disconnected", "book": "disconnected", "kline": "disconnected", "oi": "ok", "mark": "disconnected",
        "liquidations": "disconnected"}
    assert q(end - timedelta(minutes=1))["kline"]["status"] == "ok"       # the gap had not started yet
    empty = {s["stream"]: s["status"] for s in _get(client, m, instrument()["instrument_id"], "details",
                                                     to=end.isoformat())["quality"]}
    assert set(empty.values()) == {"unavailable"}


# -- observations ----------------------------------------------------------------------------------
def test_observations_are_facts_and_cvd_is_omitted_over_a_trade_gap(client, member, admin, instrument):
    inst = instrument()
    iid = inst["instrument_id"]
    end = datetime(2026, 3, 9, 10, 0, tzinfo=UTC)
    seed_futures_state(admin, inst, end - timedelta(minutes=70), end)
    seed_wall_book(admin, inst, end - timedelta(minutes=30), every_s=2, hole=None)
    w = end - timedelta(minutes=4)
    _trades(admin, inst, [(w, Decimal("100"), Decimal("1.5"), "buy"), (w + timedelta(seconds=10), Decimal("100.2"),
                                                                       Decimal("0.5"), "sell"),
                          (w + timedelta(seconds=20), Decimal("100.5"), Decimal("5"), "unknown")])
    m = member(realtime=True, admin_conn=admin)
    o = _get(client, m, iid, "observations", to=end.isoformat())
    assert ts(o["at"]) == end and len(o["items"]) <= 5
    items = {i["kind"]: i for i in o["items"]}
    assert items["cvd"]["values"] == {"cvd_change": "1", "price_change_pct": "0.50"}     # unknown side excluded
    assert items["cvd"]["units"] == {"cvd_change": inst["base_asset"]} and items["cvd"]["window_s"] == 300
    assert items["wall"]["values"] == {"side": "bid", "price": "99.5", "size": "20", "mult": "20.0",
                                       "age_s": items["wall"]["values"]["age_s"]}
    assert items["funding"]["values"] == {"rate_pct": "0.0100", "kind": "estimated"}
    assert Decimal(items["oi"]["values"]["oi_change_pct"]) > 0 and items["oi"]["evidence"] == "oi"
    assert o["items"][0]["kind"] == "wall"                       # mult 20 is the most unusual fact here
    # an unrecovered trade gap inside the window: no CVD at all (no silent partial sum)
    _gap(admin, iid, "trades", "idgap:9-9", "id_gap_detected", end - timedelta(minutes=2), end - timedelta(minutes=1))
    o2 = _get(client, m, iid, "observations", to=end.isoformat())
    assert "cvd" not in {i["kind"] for i in o2["items"]} and "wall" in {i["kind"] for i in o2["items"]}
    # nothing stored: no items, nothing zero-filled
    assert _get(client, m, instrument()["instrument_id"], "observations", to=end.isoformat())["items"] == []


# -- errors ----------------------------------------------------------------------------------------
def test_market_state_endpoints_reject_bad_requests(client, member, instrument):
    m = member()
    iid = instrument()["instrument_id"]
    for what in ("quote", "details", "observations"):
        assert client.get(f"/v1/instruments/999999999/{what}", headers=m.h).status_code == 404
        assert client.get(f"/v1/instruments/abc/{what}", headers=m.h).status_code == 422
        assert client.get(f"/v1/instruments/{iid}/{what}").status_code in (401, 403)
    bad = [{"from": "2026-10-01T00:00:00"},                                         # no timezone
           {"from": "2026-10-02T00:00:00Z", "to": "2026-10-01T00:00:00Z"},           # to before from
           {"from": "2026-10-01T00:00:00Z", "to": "2026-10-05T00:00:00Z"},           # more than 3 days
           {"from": (now_utc() - timedelta(minutes=5)).isoformat()}]                 # after a delayed cut-off
    codes = []
    for params in bad:
        r = client.get(f"/v1/instruments/{iid}/details", headers=m.h, params=params)
        assert r.status_code == 422, (params, r.text)
        codes.append(r.json().get("error", {}).get("code"))      # FastAPI's own validation body has no "error"
    assert codes[2] == "range_too_large" and codes[3] == "invalid_range"
    assert client.get(f"/v1/instruments/{iid}/observations", headers=m.h,
                      params={"to": "2026-10-01T00:00:00"}).status_code == 422
