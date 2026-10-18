"""ingest.py on a small collector-shaped fixture: idempotent re-runs, exact decimals,
taker side mapping, late arrivals, and data_quality gaps that stay until recovered."""
from __future__ import annotations

import gzip
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
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
