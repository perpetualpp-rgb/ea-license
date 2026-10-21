"""ingest.py on a small collector-shaped fixture: idempotent re-runs, exact decimals,
taker side mapping, late arrivals, and data_quality gaps that stay until recovered."""
from __future__ import annotations

import gzip
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

import ingest
from app.db import CONNECT_OPTIONS

DAY = "2026-10-17"
H10 = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)


def t(seconds: float) -> int:
    """epoch ms for 10:00:00 UTC + seconds"""
    return int((H10 + timedelta(seconds=seconds)).timestamp() * 1000)


def write(root: Path, symbol: str, stream: str, hour: str, rows, gz=True, torn: str = ""):
    d = root / "futures" / symbol / stream / DAY
    d.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows) + torn
    if gz:
        with gzip.open(d / f"{hour}.jsonl.gz", "wt", encoding="utf-8") as fh:
            fh.write(text)
    else:
        (d / f"{hour}.jsonl").write_text(text, encoding="utf-8")


def build_fixture(root: Path, symbol: str) -> None:
    def trade(i, sold=None, at=None):
        return [t(i if at is None else at), i, 4012.35 + i / 100, 0.001 * i, (i % 2) if sold is None else sold]
    write(root, symbol, "trades", "10", [trade(i, sold=2 if i == 7 else None) for i in range(1, 51) if not 21 <= i <= 25])
    # next hour: the collector's REST backfill of 21-23 (event time in hour 10), a duplicate after reconnect,
    # new trades, a hole at 70-71 that no meta row mentions, and a line torn by a crash
    write(root, symbol, "trades", "11", [trade(21), trade(22), trade(23), trade(50)]
          + [trade(i, at=3600 + i) for i in range(51, 76) if i not in (70, 71)], gz=False, torn='[1760695')
    write(root, symbol, "kline", "10", [[t(60 * m), 4012.0, 4013.5, 4011.25, 4012.75, 12.5, 40, 7.25]
                                        for m in range(10) if m != 5])
    write(root, symbol, "book", "10", [[t(s), 1000 + s, 4012.55, [[4012.5, 1.25], [4012.0, 3.5]], [[4013.0, 0.75]]]
                                       for s in range(60) if not 30 <= s <= 40])
    write(root, symbol, "meta", "10", [
        [t(0), "connected"],
        [t(30), "book_resync", 1],
        [t(41), "book_synced", 12345],
        [t(330), "disconnected", "no data for 30 s"],
        [t(370), "connected"],
        [t(372), "book_synced", 23456],
        [t(375), "trade_gap", 24, 25],
        [t(375), "backfill", 21, 25, 3],
        [t(1800), "connected"],              # restart: no disconnect was written (process stopped)
        [t(1805), "book_synced", 99999],
    ])


def _conn(dsn):
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row, options=CONNECT_OPTIONS)


def _gaps(admin, iid):
    return {(r["stream"], r["source_key"].split(":")[0]): r for r in admin.execute(
        "SELECT * FROM data_quality WHERE instrument_id = %s ORDER BY id", [iid])}


def test_ingest_is_idempotent_and_records_gaps(tmp_path, dsns, admin, client, member):
    symbol = f"X{uuid.uuid4().hex[:5].upper()}USDT"
    build_fixture(tmp_path, symbol)
    cfg = {symbol: {"bucket": 0.5, "window_pct": 1.0}}
    with _conn(dsns["ingest"]) as conn:                    # runs as the ingestion role, not the owner
        first = ingest.Ingestor(conn, tmp_path, book_cfg=cfg).run()
    assert first["trades_inserted"] == 45 + 3 + 23          # 50 - 5 missing, 3 backfilled, 51..75 minus 70-71
    assert first["kline_inserted"] == 9 and first["book_inserted"] == 49 and first["meta_inserted"] == 10
    assert first["rows_rejected"] == 1                      # the torn line

    inst = admin.execute("SELECT * FROM instruments WHERE venue = 'binance' AND symbol = %s", [symbol]).fetchone()
    iid = inst["instrument_id"]
    assert (inst["market_type"], inst["contract_type"], inst["base_asset"], inst["quote_asset"], inst["spec_status"]) \
        == ("perpetual", "linear", "X" + symbol[1:6], "USDT", "unverified")
    assert inst["tick_size"] is None                         # unknown, not guessed

    # exact decimals, taker side, late arrival
    row = admin.execute("SELECT price::text, qty::text, taker_side, quality FROM trades WHERE instrument_id = %s"
                        " AND agg_trade_id = 21", [iid]).fetchone()
    assert row == {"price": "4012.56", "qty": "0.021", "taker_side": "sell", "quality": "late_arrival"}
    sides = {r["agg_trade_id"]: r["taker_side"] for r in admin.execute(
        "SELECT agg_trade_id, taker_side FROM trades WHERE instrument_id = %s AND agg_trade_id IN (2, 3, 7)", [iid])}
    assert sides == {2: "buy", 3: "sell", 7: "unknown"}     # 1 = taker sold; anything else is not a sell
    book = admin.execute("SELECT bids, asks, bucket_size::text, last_update_id FROM book_snapshots WHERE instrument_id = %s"
                         " ORDER BY captured_ts LIMIT 1", [iid]).fetchone()
    assert book == {"bids": [["4012.5", "1.25"], ["4012.0", "3.5"]], "asks": [["4013.0", "0.75"]],
                    "bucket_size": "0.5", "last_update_id": 1000}

    g = _gaps(admin, iid)
    assert g[("book", "resync")]["status"] == "unrecoverable"
    assert (g[("book", "resync")]["gap_start"], g[("book", "resync")]["gap_end"]) == \
        (H10 + timedelta(seconds=30), H10 + timedelta(seconds=41))
    assert g[("book", "disconnect")]["gap_end"] == H10 + timedelta(seconds=372)      # until the book re-synced
    k = g[("kline", "disconnect")]
    assert (k["status"], k["expected_count"], k["present_count"]) == ("unrecoverable", 1, 0)   # the 10:05 candle
    b = g[("trades", "backfill")]
    assert (b["status"], b["first_missing_id"], b["last_missing_id"], b["present_count"]) == ("partial", 21, 25, 3)
    assert g[("trades", "trade_gap")]["status"] == "unrecoverable"
    restart = g[("kline", "restart")]
    assert restart["gap_start"] == H10 + timedelta(minutes=10) and restart["gap_end"] == H10 + timedelta(minutes=30)
    assert (restart["expected_count"], restart["present_count"]) == (20, 0)
    assert g[("book", "restart")]["gap_end"] == H10 + timedelta(seconds=1805)
    hole = g[("trades", "idgap")]
    assert (hole["status"], hole["first_missing_id"], hole["last_missing_id"]) == ("open", 70, 71)

    # re-running changes nothing
    with _conn(dsns["ingest"]) as conn:
        again = ingest.Ingestor(conn, tmp_path, book_cfg=cfg).run()
        forced = ingest.Ingestor(conn, tmp_path, book_cfg=cfg, force=True).run()
    assert again.get("files_loaded", 0) == 0 and again["files_unchanged"] == 5
    assert sum(v for k, v in forced.items() if k.endswith("_inserted")) == 0 and forced["files_loaded"] == 5
    counts = admin.execute("SELECT (SELECT count(*) FROM trades WHERE instrument_id = %(i)s) AS t,"
                           " (SELECT count(*) FROM raw_events WHERE instrument_id = %(i)s) AS r,"
                           " (SELECT count(*) FROM data_quality WHERE instrument_id = %(i)s) AS q", {"i": iid}).fetchone()
    assert counts == {"t": 71, "r": 10, "q": len(g)}

    # the missing ids arrive later (another late file): gaps flip to recovered, never silently disappear
    (tmp_path / "futures" / symbol / "trades" / DAY / "12.jsonl").write_text(
        "".join(json.dumps([t(s), s, 4012.0, 0.5, 0]) + "\n" for s in (24, 25, 70, 71)), encoding="utf-8")
    with _conn(dsns["ingest"]) as conn:
        ingest.Ingestor(conn, tmp_path, book_cfg=cfg).run()
    g2 = _gaps(admin, iid)
    assert len(g2) == len(g)
    for key in (("trades", "backfill"), ("trades", "trade_gap"), ("trades", "idgap")):
        assert g2[key]["status"] == "recovered" and g2[key]["repaired_at"] is not None, key
    assert g2[("kline", "disconnect")]["status"] == "unrecoverable"

    # the API serves what was ingested
    m = member(realtime=True, admin_conn=admin)
    bars = client.get(f"/v1/instruments/{iid}/bars", headers=m.h, params={
        "tf": "1m", "from": H10.isoformat(), "to": (H10 + timedelta(minutes=10)).isoformat()}).json()
    assert len(bars["bars"]) == 9 and bars["bars"][0]["close"] == "4012.75" and bars["bars"][0]["delta"] == "2"
    q = client.get(f"/v1/instruments/{iid}/quality", headers=m.h, params={
        "from": H10.isoformat(), "to": (H10 + timedelta(hours=1)).isoformat()}).json()
    assert {(x["stream"], x["status"]) for x in q["gaps"]} >= {("kline", "unrecoverable"), ("trades", "recovered")}
    assert q["streams"]["kline"]["present"] == 9 and q["streams"]["kline"]["expected"] == 60


def test_split_symbol_and_path_filter(tmp_path):
    assert ingest.split_symbol("BTCUSDT") == ("BTC", "USDT")
    assert ingest.split_symbol("ETHBTC") == ("ETH", "BTC")
    (tmp_path / "futures" / "BTCUSDT" / "trades" / DAY).mkdir(parents=True)
    (tmp_path / "futures" / "BTCUSDT" / "trades" / DAY / "10.jsonl").write_text("")
    (tmp_path / "status.json").write_text("{}")
    (tmp_path / "state.json").write_text("{}")
    assert [rel for _, rel, _ in ingest.iter_files(tmp_path)] == [f"futures/BTCUSDT/trades/{DAY}/10.jsonl"]


def test_ingest_mark_oi_liq_into_typed_tables_idempotently(tmp_path, dsns, admin, client, member):
    symbol = f"M{uuid.uuid4().hex[:5].upper()}USDT"
    nxt = t(3600 * 6)
    write(tmp_path, symbol, "mark", "10", [
        [t(0), 4012.35, 4011.9, 0.0001, nxt],
        [t(5), 4012.4, 0, 0.00003866, nxt],          # index 0 = the stream sent none
        [t(5), 4012.4, 0, 0.00003866, nxt],          # same event written twice (stalled stream): one row
        [t(10), 4012.5, 4012.1, 0, 0],               # no next funding time: rate unknown, not 0
        [t(15), 0, 4012.0, 0.0001, nxt],             # mark 0: rejected from the typed table
    ])
    write(tmp_path, symbol, "oi", "10", [[t(0), 84211.3], [t(60), 84220.125], [t(120), 84230.0]], gz=False)
    write(tmp_path, symbol, "liq", "10", [
        [t(3), "SELL", 4010.5, 4010.25, 1.5, "FILLED"],
        [t(4), "BUY", 4013.0, 0, 0.25, "NEW"],
        [t(4), "BUY", 4013.0, 0, 0.25, "NEW"],
    ])
    with _conn(dsns["ingest"]) as conn:                      # ingestion role (0002 grants INSERT)
        first = ingest.Ingestor(conn, tmp_path).run()
    assert (first["mark_normalized_inserted"], first["oi_normalized_inserted"], first["liq_normalized_inserted"]) == (3, 3, 2)
    assert (first["mark_inserted"], first["oi_inserted"], first["liq_inserted"]) == (4, 3, 2)   # raw copy kept as before
    assert first["rows_rejected"] == 1
    iid = admin.execute("SELECT instrument_id FROM instruments WHERE venue = 'binance' AND symbol = %s",
                        [symbol]).fetchone()["instrument_id"]
    marks = admin.execute("SELECT mark_price::text AS m, index_price::text AS i, funding_rate::text AS f,"
                          " next_funding_ts AS n FROM mark_prices WHERE instrument_id = %s ORDER BY event_ts",
                          [iid]).fetchall()
    assert marks == [{"m": "4012.35", "i": "4011.9", "f": "0.0001", "n": H10 + timedelta(hours=6)},
                     {"m": "4012.4", "i": None, "f": "0.00003866", "n": H10 + timedelta(hours=6)},
                     {"m": "4012.5", "i": "4012.1", "f": None, "n": None}]
    assert [r["v"] for r in admin.execute("SELECT open_interest::text AS v FROM open_interest WHERE instrument_id = %s"
                                          " ORDER BY event_ts", [iid])] == ["84211.3", "84220.125", "84230.0"]
    liq = admin.execute("SELECT side, price::text AS p, avg_price::text AS a, qty::text AS q, status FROM liquidations"
                        " WHERE instrument_id = %s ORDER BY event_ts", [iid]).fetchall()
    assert liq == [{"side": "sell", "p": "4010.5", "a": "4010.25", "q": "1.5", "status": "FILLED"},
                   {"side": "buy", "p": "4013.0", "a": None, "q": "0.25", "status": "NEW"}]

    def counts():
        return admin.execute("SELECT (SELECT count(*) FROM mark_prices WHERE instrument_id = %(i)s) AS m,"
                             " (SELECT count(*) FROM open_interest WHERE instrument_id = %(i)s) AS o,"
                             " (SELECT count(*) FROM liquidations WHERE instrument_id = %(i)s) AS l,"
                             " (SELECT count(*) FROM raw_events WHERE instrument_id = %(i)s) AS r", {"i": iid}).fetchone()
    before = counts()
    with _conn(dsns["ingest"]) as conn:
        again = ingest.Ingestor(conn, tmp_path).run()
        forced = ingest.Ingestor(conn, tmp_path, force=True).run()
    assert again.get("files_loaded", 0) == 0 and forced["files_loaded"] == 3
    assert sum(v for k, v in forced.items() if k.endswith("_inserted")) == 0
    assert counts() == before == {"m": 3, "o": 3, "l": 2, "r": 9}

    # the API reads the typed rows
    m = member(realtime=True, admin_conn=admin)
    d = client.get(f"/v1/instruments/{iid}/details", headers=m.h, params={
        "from": H10.isoformat(), "to": (H10 + timedelta(minutes=3)).isoformat()}).json()
    assert d["oi"]["value"] == "84230" and d["oi"]["change_1h_pct"] is None
    assert d["funding"] is None                                # newest mark row has no known rate
    assert {p["type"]: p["value"] for p in d["prices"]}["mark"] == "4012.5"
    assert next(s for s in d["quality"] if s["stream"] == "liquidations")["last_at"] == "2026-10-17T10:00:04Z"


def _rest_restart_fixture(root: Path, symbol: str) -> None:
    """Collector stops at 10:10:30 and restarts at 10:30:01. On start it fetched the minutes it missed
    over REST (kline_history 10:10-10:29, closes up to 10:30:00) and back-filled the missing trade ids
    64..179 (event times up to 10:29:50, filed in the same hour, so quality 'ok')."""
    ws_trades = [[t(10 * i), i, 4012.0, 0.5, i % 2] for i in range(1, 64)]                  # up to 10:10:30
    rest_trades = [[t(630 + 10 * k), 63 + k, 4012.5, 0.25, 0] for k in range(1, 117)]          # ids 64..179
    after = [[t(1810 + k), 179 + k, 4013.0, 0.5, 1] for k in range(1, 11)]
    write(root, symbol, "trades", "10", ws_trades + rest_trades + after)
    kl = lambda m: [t(60 * m), 4012.0, 4013.0, 4011.0, 4012.5, 3.0, 12, 1.5]  # noqa: E731
    write(root, symbol, "kline", "10", [kl(m) for m in range(10)] + [kl(m) for m in range(10, 30)]
          + [kl(30), kl(31)])
    write(root, symbol, "book", "10", [[t(s), 1000 + s, 4012.55, [[4012.5, 1.0]], [[4013.0, 1.0]]]
                                       for s in list(range(0, 631, 10)) + list(range(1802, 1900, 2))])
    write(root, symbol, "meta", "10", [
        [t(0), "connected"],
        [t(1800), "kline_history", t(600), t(1740), 20],
        [t(1801), "connected"],
        [t(1802), "book_synced", 5],
        [t(1830), "backfill", 64, 179, 116],
    ])


def test_rest_klines_and_backfilled_trades_do_not_hide_a_restart(tmp_path, dsns, admin):
    symbol = f"R{uuid.uuid4().hex[:5].upper()}USDT"
    _rest_restart_fixture(tmp_path, symbol)
    with _conn(dsns["ingest"]) as conn:
        stats = ingest.Ingestor(conn, tmp_path).run()
    assert stats["klines_tagged_rest"] == 20
    iid = admin.execute("SELECT instrument_id FROM instruments WHERE venue = 'binance' AND symbol = %s",
                        [symbol]).fetchone()["instrument_id"]
    src = {r["m"]: r["source"] for r in admin.execute(
        "SELECT extract(epoch FROM open_time - %s)::int / 60 AS m, source FROM klines WHERE instrument_id = %s",
        [H10, iid])}
    assert {m for m, s in src.items() if s == "rest_backfill"} == set(range(10, 30))
    assert {m for m, s in src.items() if s == "exchange_kline"} == set(range(10)) | {30, 31}
    g = _gaps(admin, iid)
    # the outage starts at the last WebSocket data (10:10:30), not at the newest REST candle or trade
    book = g[("book", "restart")]
    assert (book["gap_start"], book["gap_end"]) == (H10 + timedelta(seconds=630), H10 + timedelta(seconds=1802))
    assert book["status"] == "unrecoverable"
    kline = g[("kline", "restart")]
    assert kline["gap_start"] == H10 + timedelta(seconds=630)
    assert (kline["status"], kline["expected_count"], kline["present_count"]) == ("recovered", 20, 20)  # REST filled it

    # re-running re-tags nothing and adds nothing
    before = admin.execute("SELECT count(*) AS n FROM data_quality WHERE instrument_id = %s", [iid]).fetchone()["n"]
    with _conn(dsns["ingest"]) as conn:
        forced = ingest.Ingestor(conn, tmp_path, force=True).run()
    assert forced.get("klines_tagged_rest", 0) == 0 and sum(v for k, v in forced.items() if k.endswith("_inserted")) == 0
    assert admin.execute("SELECT count(*) AS n FROM data_quality WHERE instrument_id = %s", [iid]).fetchone()["n"] == before


def test_ingest_role_may_only_retag_kline_source(dsns, admin, instrument):
    from conftest import seed_market
    inst = instrument()
    seed_market(admin, inst, minutes=2)
    with _conn(dsns["ingest"]) as conn:
        conn.execute("UPDATE klines SET source = 'rest_backfill' WHERE instrument_id = %s", [inst["instrument_id"]])
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE klines SET close = 1 WHERE instrument_id = %s", [inst["instrument_id"]])
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE klines SET source = 'made_up' WHERE instrument_id = %s", [inst["instrument_id"]])


def test_kline_gap_from_rest_is_recorded_and_recovers_when_filled(tmp_path, dsns, admin):
    symbol = f"G{uuid.uuid4().hex[:5].upper()}USDT"
    kl = lambda at: [at, 4012.0, 4013.0, 4011.0, 4012.5, 3.0, 12, 1.5]  # noqa: E731
    h9 = lambda m: t(-3600 + 60 * m)  # noqa: E731  open time 09:mm
    # start at 10:00: the history fetch for 09:00-09:29 failed, 09:30-09:59 arrived, then the socket connected
    write(tmp_path, symbol, "kline", "10", [kl(h9(m)) for m in range(30, 60)] + [kl(t(60 * m)) for m in range(5)])
    write(tmp_path, symbol, "meta", "10", [
        [t(1), "kline_retry", h9(0), h9(59), h9(0), 6, "timeout"],
        [t(1), "kline_gap", h9(0), h9(29), "REST failed 6 times: timeout"],
        [t(2), "kline_history", h9(30), h9(59), 30],
        [t(3), "connected"],
    ])
    with _conn(dsns["ingest"]) as conn:
        ingest.Ingestor(conn, tmp_path).run()
    iid = admin.execute("SELECT instrument_id FROM instruments WHERE venue = 'binance' AND symbol = %s",
                        [symbol]).fetchone()["instrument_id"]
    gap = _gaps(admin, iid)[("kline", "kline_gap")]
    assert (gap["reason"], gap["status"], gap["expected_count"], gap["present_count"]) == \
        ("collector_kline_gap", "unrecoverable", 30, 0)
    assert (gap["gap_start"], gap["gap_end"]) == (H10 - timedelta(hours=1), H10 - timedelta(minutes=30))
    assert gap["details"] == {"reason": "REST failed 6 times: timeout"} and gap["repaired_at"] is None

    # the next start's history fetch fills those minutes (rows filed in a later hour, by the collector clock)
    write(tmp_path, symbol, "kline", "11", [kl(h9(m)) for m in range(30)])
    write(tmp_path, symbol, "meta", "11", [[t(3600), "kline_history", h9(0), h9(29), 30], [t(3601), "connected"]])
    with _conn(dsns["ingest"]) as conn:
        ingest.Ingestor(conn, tmp_path).run()
    gap = _gaps(admin, iid)[("kline", "kline_gap")]
    assert (gap["status"], gap["present_count"]) == ("recovered", 30) and gap["repaired_at"] is not None
    assert admin.execute("SELECT count(*) AS n FROM klines WHERE instrument_id = %s AND source = 'rest_backfill'",
                         [iid]).fetchone()["n"] == 60
