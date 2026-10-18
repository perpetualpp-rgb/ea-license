#!/usr/bin/env python3
"""Load the phase-0 collector's hourly files into PostgreSQL, idempotently.

    FLOWLAB_INGEST_DATABASE_URL=postgresql://flowlab_ingestor@127.0.0.1/flowlab \\
        python3 ingest.py --data ../data/live --collector-config ../live/config.json

Input layout (live/sink.py):  <data>/<market>/<symbol>/<stream>/<YYYY-MM-DD>/<HH>.jsonl[.gz]
Rows (live/collector.py):
    trades [T, aggId, price, qty, takerSold]   -> trades       key venue+market+symbol+agg_trade_id
    kline  [t, o, h, l, c, v, n, takerBuyV]    -> klines       key instrument+interval+open_time
    book   [ts, updateId, mid, bids, asks]     -> book_snapshots key instrument+kind+captured_ts
    meta   [ts, event, ...]                    -> raw_events (+ data_quality gaps)
    mark / oi / liq                            -> raw_events  key UUIDv5 of the source line
                                                  and a typed copy (migration 0002):
    mark   [E, mark, index, funding, nextFunding] -> mark_prices   key instrument+event_ts
    oi     [time, openInterest]                -> open_interest key instrument+event_ts
    liq    [T, side, price, avgPrice, qty, status] -> liquidations key instrument+T+side+price+qty

Re-running inserts nothing new (ON CONFLICT DO NOTHING on business keys); a file whose
checksum is unchanged since the last run is skipped (manifest in ingested_files). Rows
backfilled by the collector sit in later hour files than their event time, so files are
never assumed to hold one hour of events: everything is keyed by the timestamp inside.
Numbers are parsed as Decimal and stored as NUMERIC (no float round-trip).

Gaps (data_quality) come from two sources and keep their label until the data is there:
* meta events: disconnect/reconnect windows and restarts (book, kline), book resyncs,
  the collector's backfill and trade_gap reports (trade id ranges), kline_gap reports
  (minutes the collector could not fetch over REST);
* a scan for missing aggregate trade ids around the ids loaded in this run.
1m klines the collector fetched over REST (meta kline_history / kline_backfill) are tagged
klines.source = 'rest_backfill'; they, and trades in a backfill id range, are not taken as
evidence that the collector was receiving when a restart is measured.
This script never contacts an exchange.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

TOOL_VERSION = "ingest/0.2.0"
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
RAW_NS = uuid.UUID("0b7c2f4e-9d1a-5c3b-8e6f-1a2b3c4d5e6f")
PATH_RE = re.compile(r"^(?P<market>[a-z]+)/(?P<symbol>[A-Z0-9]+)/(?P<stream>[a-z]+)/"
                     r"(?P<day>\d{4}-\d{2}-\d{2})/(?P<hour>\d{2})\.jsonl(?:\.gz)?$")
MARKETS = {"futures": ("perpetual", "linear"), "spot": ("spot", "spot")}
QUOTES = ("USDT", "USDC", "FDUSD", "BUSD", "TUSD", "BTC", "ETH", "BNB", "EUR", "TRY", "BRL")
RAW_STREAMS = {"meta", "mark", "oi", "liq"}
NORMALIZED = {"mark": "load_mark", "oi": "load_oi", "liq": "load_liq"}   # also kept in raw_events
STREAMS = {"trades", "kline", "book"} | RAW_STREAMS
MIN_RESTART_GAP = timedelta(seconds=2)


def ms(v) -> datetime:
    return EPOCH + timedelta(milliseconds=int(v))


def dtext(v) -> str:
    d = v if isinstance(v, Decimal) else Decimal(str(v))
    return format(d, "f")


def split_symbol(symbol: str) -> Tuple[str, str]:
    for q in QUOTES:
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)], q
    raise ValueError(f"cannot split {symbol} into base/quote; add its quote asset to QUOTES")


def iter_files(root: Path) -> Iterator[Tuple[Path, str, dict]]:
    for dirpath, _, files in os.walk(root):
        for f in sorted(files):
            p = Path(dirpath) / f
            rel = p.relative_to(root).as_posix()
            m = PATH_RE.match(rel)
            if m and m["stream"] in STREAMS:
                yield p, rel, m.groupdict()


def file_digest(path: Path) -> Tuple[int, str]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return size, h.hexdigest()


def read_lines(path: Path) -> Iterator[str]:
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        try:
            for line in fh:
                line = line.strip()
                if line:
                    yield line
        except (EOFError, gzip.BadGzipFile):
            return  # truncated gzip member from a crash: keep what was readable


def parse(line: str):
    try:
        row = json.loads(line, parse_float=Decimal)
    except json.JSONDecodeError:
        return None
    return row if isinstance(row, list) and row else None


class Ingestor:
    def __init__(self, conn: psycopg.Connection, root: Path, venue: str = "binance",
                 book_cfg: Optional[dict] = None, force: bool = False):
        self.conn, self.root, self.venue = conn, root, venue
        self.book_cfg = book_cfg or {}
        self.force = force
        self.run_id = uuid.uuid4()
        self.instruments: Dict[Tuple[str, str], int] = {}
        self.id_ranges: Dict[int, List[int]] = {}
        self.touched: set = set()
        self.stats = defaultdict(int)

    # -- instruments ----------------------------------------------------------------
    def instrument(self, market: str, symbol: str) -> int:
        key = (market, symbol)
        if key in self.instruments:
            return self.instruments[key]
        market_type, contract_type = MARKETS[market]
        base, quote = split_symbol(symbol)
        with self.conn.transaction():
            self.conn.execute(
                "INSERT INTO instruments (venue, symbol, market_type, contract_type, base_asset, quote_asset,"
                " settlement_asset, contract_multiplier, multiplier_unit, spec_status, spec_source)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 'base', 'unverified', %s)"
                " ON CONFLICT (venue, market_type, symbol) DO NOTHING",
                [self.venue, symbol, market_type, contract_type, base, quote, quote,
                 "inferred from the collector folder name; verify tick size, step and contract size"])
            iid = self.conn.execute("SELECT instrument_id FROM instruments WHERE venue = %s AND market_type = %s"
                                    " AND symbol = %s", [self.venue, market_type, symbol]).fetchone()["instrument_id"]
        self.instruments[key] = iid
        return iid

    # -- run ------------------------------------------------------------------------------
    def run(self) -> dict:
        with self.conn.transaction():
            self.conn.execute("INSERT INTO ingestion_runs (run_id, tool, tool_version, source_root) VALUES (%s, %s, %s, %s)",
                              [self.run_id, "ingest.py", TOOL_VERSION, str(self.root)])
        status = "failed"
        try:
            for path, rel, info in iter_files(self.root):
                self.load_file(path, rel, info)
            for iid in sorted(self.touched):
                with self.conn.transaction():
                    self.derive_quality(iid)
            status = "succeeded"
        finally:
            with self.conn.transaction():
                self.conn.execute("UPDATE ingestion_runs SET finished_at = now(), status = %s, stats = %s WHERE run_id = %s",
                                  [status, Jsonb(dict(self.stats)), self.run_id])
        return dict(self.stats)

    def load_file(self, path: Path, rel: str, info: dict) -> None:
        size, digest = file_digest(path)
        prev = self.conn.execute("SELECT sha256 FROM ingested_files WHERE relpath = %s", [rel]).fetchone()
        if prev and prev["sha256"] == digest and not self.force:
            self.stats["files_unchanged"] += 1
            return
        market, symbol, stream = info["market"], info["symbol"], info["stream"]
        if market not in MARKETS:
            self.stats["files_unknown_market"] += 1
            return
        iid = self.instrument(market, symbol)
        file_hour = datetime.fromisoformat(f"{info['day']}T{info['hour']}:00:00+00:00")
        rows, rejected = [], 0
        for line in read_lines(path):
            r = parse(line)
            if r is None:
                rejected += 1
                continue
            rows.append((line, r))
        with self.conn.transaction():
            if stream == "trades":
                n, bad = self.load_trades(iid, market, symbol, rows, file_hour)
            elif stream == "kline":
                n, bad = self.load_klines(iid, rows)
            elif stream == "book":
                n, bad = self.load_book(iid, symbol, rows)
            else:
                n, bad = self.load_raw(iid, market, symbol, stream, rows, rel)
                if stream in NORMALIZED:
                    # every row the raw copy rejected is rejected here too (both need a valid time)
                    n_norm, bad = getattr(self, NORMALIZED[stream])(iid, rows)
                    self.stats[f"{stream}_normalized_inserted"] += n_norm
            rejected += bad
            self.conn.execute(
                "INSERT INTO ingested_files (relpath, size_bytes, sha256, rows_read, rows_rejected, last_run_id)"
                " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (relpath) DO UPDATE SET size_bytes = EXCLUDED.size_bytes,"
                " sha256 = EXCLUDED.sha256, rows_read = EXCLUDED.rows_read, rows_rejected = EXCLUDED.rows_rejected,"
                " last_run_id = EXCLUDED.last_run_id, ingested_at = now()",
                [rel, size, digest, len(rows), rejected, self.run_id])
        self.touched.add(iid)
        self.stats["files_loaded"] += 1
        self.stats[f"{stream}_inserted"] += n
        self.stats[f"{stream}_rows_read"] += len(rows)
        self.stats["rows_rejected"] += rejected

    def _copy_insert(self, tmp_cols: str, target: str, cols: List[str], data: List[tuple], conflict: str) -> int:
        if not data:
            return 0
        tmp = f"tmp_{target}"
        self.conn.execute(f"CREATE TEMP TABLE IF NOT EXISTS {tmp} ({tmp_cols}) ON COMMIT DELETE ROWS")
        with self.conn.cursor() as cur:
            with cur.copy(f"COPY {tmp} ({', '.join(cols)}) FROM STDIN") as cp:
                for rec in data:
                    cp.write_row(rec)
            cur.execute(f"INSERT INTO {target} ({', '.join(cols)}) SELECT {', '.join(cols)} FROM {tmp}"
                        f" ON CONFLICT {conflict} DO NOTHING")
            return cur.rowcount

    def load_trades(self, iid: int, market: str, symbol: str, rows, file_hour: datetime) -> Tuple[int, int]:
        data, bad = [], 0
        market_type = MARKETS[market][0]
        ids = self.id_ranges.setdefault(iid, [])
        for _, r in rows:
            try:
                t, agg, price, qty, sold = r[0], int(r[1]), r[2], r[3], r[4]
                ts = ms(t)
                side = "sell" if sold == 1 else "buy" if sold == 0 else "unknown"
                quality = "late_arrival" if ts < file_hour else "ok"
                data.append((self.venue, market_type, symbol, agg, iid, ts, dtext(price), dtext(qty), side, quality,
                             self.run_id))
                ids.append(agg)
            except (IndexError, TypeError, ValueError, ArithmeticError):
                bad += 1
        if ids:
            self.id_ranges[iid] = [min(ids), max(ids)]
        cols = ["venue", "market_type", "symbol", "agg_trade_id", "instrument_id", "event_ts", "price", "qty",
                "taker_side", "quality", "ingestion_run_id"]
        n = self._copy_insert("venue text, market_type text, symbol text, agg_trade_id bigint, instrument_id bigint,"
                              " event_ts timestamptz, price numeric, qty numeric, taker_side text, quality text,"
                              " ingestion_run_id uuid", "trades", cols, data,
                              "(venue, market_type, symbol, agg_trade_id)")
        return n, bad

    def load_klines(self, iid: int, rows) -> Tuple[int, int]:
        data, bad = [], 0
        for _, r in rows:
            try:
                data.append((iid, 60, ms(r[0]), dtext(r[1]), dtext(r[2]), dtext(r[3]), dtext(r[4]), dtext(r[5]),
                             int(r[6]), dtext(r[7]), self.run_id))
            except (IndexError, TypeError, ValueError, ArithmeticError):
                bad += 1
        cols = ["instrument_id", "interval_s", "open_time", "open", "high", "low", "close", "volume", "trade_count",
                "taker_buy_volume", "ingestion_run_id"]
        return self._copy_insert("instrument_id bigint, interval_s integer, open_time timestamptz, open numeric,"
                                 " high numeric, low numeric, close numeric, volume numeric, trade_count integer,"
                                 " taker_buy_volume numeric, ingestion_run_id uuid", "klines", cols, data,
                                 "(instrument_id, interval_s, open_time)"), bad

    def load_book(self, iid: int, symbol: str, rows) -> Tuple[int, int]:
        cfg = self.book_cfg.get(symbol, {})
        data, bad = [], 0
        for _, r in rows:
            try:
                bids = json.dumps([[dtext(p), dtext(q)] for p, q in r[3]])
                asks = json.dumps([[dtext(p), dtext(q)] for p, q in r[4]])
                data.append((iid, "bucket_summary", ms(r[0]), int(r[1]), dtext(r[2]), bids, asks,
                             dtext(cfg["bucket"]) if "bucket" in cfg else None,
                             dtext(cfg["window_pct"]) if "window_pct" in cfg else None, True, self.run_id))
            except (IndexError, TypeError, ValueError, ArithmeticError):
                bad += 1
        cols = ["instrument_id", "kind", "captured_ts", "last_update_id", "mid", "bids", "asks", "bucket_size",
                "window_pct", "synced", "ingestion_run_id"]
        return self._copy_insert("instrument_id bigint, kind text, captured_ts timestamptz, last_update_id bigint,"
                                 " mid numeric, bids jsonb, asks jsonb, bucket_size numeric, window_pct numeric,"
                                 " synced boolean, ingestion_run_id uuid", "book_snapshots", cols, data,
                                 "(instrument_id, kind, captured_ts)"), bad

    def load_raw(self, iid: int, market: str, symbol: str, stream: str, rows, rel: str) -> Tuple[int, int]:
        data, bad = [], 0
        for line, r in rows:
            try:
                ts = ms(r[0])
            except (TypeError, ValueError, ArithmeticError):
                bad += 1
                continue
            eid = uuid.uuid5(RAW_NS, f"{self.venue}|{market}|{symbol}|{stream}|{line}")
            received, exchange = (ts, None) if stream == "meta" else (None, ts)
            data.append((eid, iid, stream, exchange, received, 1, line, self.run_id, rel))
        cols = ["event_id", "instrument_id", "stream", "exchange_event_ts", "received_ts", "schema_version", "payload",
                "ingestion_run_id", "source_file"]
        return self._copy_insert("event_id uuid, instrument_id bigint, stream text, exchange_event_ts timestamptz,"
                                 " received_ts timestamptz, schema_version integer, payload jsonb,"
                                 " ingestion_run_id uuid, source_file text", "raw_events", cols, data,
                                 "(event_id)"), bad

    # -- futures market state (typed copies of the raw rows) -------------------------------
    def load_mark(self, iid: int, rows) -> Tuple[int, int]:
        """[E, mark, index, funding, nextFunding]. The collector writes 0 for a field the stream
        did not send: index 0 -> NULL, and without a next funding time the rate is unknown -> NULL."""
        data, bad = [], 0
        for _, r in rows:
            try:
                ts, mark = ms(r[0]), Decimal(str(r[1]))
                if not mark.is_finite() or mark <= 0:
                    raise ValueError("mark price must be > 0")
                index = Decimal(str(r[2])) if len(r) > 2 and r[2] is not None else None
                index = index if index is not None and index.is_finite() and index > 0 else None
                nxt = int(r[4]) if len(r) > 4 and r[4] else 0
                rate = Decimal(str(r[3])) if nxt and len(r) > 3 and r[3] is not None else None
                if rate is not None and not rate.is_finite():
                    rate = None
                data.append((iid, ts, dtext(mark), dtext(index) if index is not None else None,
                             dtext(rate) if rate is not None else None, ms(nxt) if nxt else None, self.run_id))
            except (IndexError, TypeError, ValueError, ArithmeticError):
                bad += 1
        cols = ["instrument_id", "event_ts", "mark_price", "index_price", "funding_rate", "next_funding_ts",
                "ingestion_run_id"]
        return self._copy_insert("instrument_id bigint, event_ts timestamptz, mark_price numeric, index_price numeric,"
                                 " funding_rate numeric, next_funding_ts timestamptz, ingestion_run_id uuid",
                                 "mark_prices", cols, data, "(instrument_id, event_ts)"), bad

    def load_oi(self, iid: int, rows) -> Tuple[int, int]:
        """[time, openInterest]"""
        data, bad = [], 0
        for _, r in rows:
            try:
                ts, oi = ms(r[0]), Decimal(str(r[1]))
                if not oi.is_finite() or oi < 0:
                    raise ValueError("open interest must be >= 0")
                data.append((iid, ts, dtext(oi), self.run_id))
            except (IndexError, TypeError, ValueError, ArithmeticError):
                bad += 1
        return self._copy_insert("instrument_id bigint, event_ts timestamptz, open_interest numeric,"
                                 " ingestion_run_id uuid", "open_interest",
                                 ["instrument_id", "event_ts", "open_interest", "ingestion_run_id"], data,
                                 "(instrument_id, event_ts)"), bad

    def load_liq(self, iid: int, rows) -> Tuple[int, int]:
        """[T, side, price, avgPrice, qty, status]; side is the liquidation order's side."""
        data, bad = [], 0
        for _, r in rows:
            try:
                ts = ms(r[0])
                side = {"SELL": "sell", "BUY": "buy"}.get(str(r[1]).upper(), "unknown")
                price, avg, qty = Decimal(str(r[2])), Decimal(str(r[3])), Decimal(str(r[4]))
                if not (price.is_finite() and qty.is_finite() and avg.is_finite()) or price <= 0 or qty <= 0:
                    raise ValueError("price and qty must be > 0")
                status = str(r[5]) if len(r) > 5 and r[5] is not None else "unknown"
                data.append((iid, ts, side, dtext(price), dtext(avg) if avg > 0 else None, dtext(qty), status[:40],
                             self.run_id))
            except (IndexError, TypeError, ValueError, ArithmeticError):
                bad += 1
        return self._copy_insert("instrument_id bigint, event_ts timestamptz, side text, price numeric,"
                                 " avg_price numeric, qty numeric, status text, ingestion_run_id uuid", "liquidations",
                                 ["instrument_id", "event_ts", "side", "price", "avg_price", "qty", "status",
                                  "ingestion_run_id"], data, "(instrument_id, event_ts, side, price, qty)"), bad

    # -- data quality -------------------------------------------------------------------
    def _upsert_gap(self, iid: int, stream: str, key: str, reason: str, status: str, start, end,
                    first_id=None, last_id=None, expected=None, present=None, details=None) -> None:
        if start is not None and end is not None and end < start:
            start, end = end, start
        self.conn.execute(
            """
            INSERT INTO data_quality (instrument_id, stream, source_key, reason, status, gap_start, gap_end,
                first_missing_id, last_missing_id, expected_count, present_count, repaired_at, details)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s = 'recovered' THEN now() END, %s)
            ON CONFLICT (instrument_id, stream, source_key) DO UPDATE SET
                status = EXCLUDED.status, gap_start = EXCLUDED.gap_start, gap_end = EXCLUDED.gap_end,
                expected_count = EXCLUDED.expected_count, present_count = EXCLUDED.present_count,
                details = EXCLUDED.details,
                repaired_at = CASE WHEN EXCLUDED.status = 'recovered'
                                   THEN coalesce(data_quality.repaired_at, now()) END
            """, [iid, stream, key, reason, status, start, end, first_id, last_id, expected, present, status,
                  Jsonb(details or {})])
        self.stats[f"gaps_{stream}"] += 1

    def _id_range_status(self, iid: int, lo: int, hi: int, final: bool) -> Tuple[str, int, int, datetime, datetime]:
        expected = hi - lo + 1
        present = self.conn.execute("SELECT count(*) AS n FROM trades WHERE instrument_id = %s"
                                    " AND agg_trade_id BETWEEN %s AND %s", [iid, lo, hi]).fetchone()["n"]
        before = self.conn.execute("SELECT event_ts FROM trades WHERE instrument_id = %s AND agg_trade_id < %s"
                                   " ORDER BY agg_trade_id DESC LIMIT 1", [iid, lo]).fetchone()
        after = self.conn.execute("SELECT event_ts FROM trades WHERE instrument_id = %s AND agg_trade_id > %s"
                                  " ORDER BY agg_trade_id LIMIT 1", [iid, hi]).fetchone()
        status = ("recovered" if present >= expected else "partial" if present > 0
                  else "unrecoverable" if final else "open")
        return status, expected, present, before["event_ts"] if before else None, after["event_ts"] if after else None

    def _kline_window(self, iid: int, start: datetime, end: Optional[datetime]) -> Tuple[int, int]:
        """Closed 1m candles whose close time falls inside (start, end]: expected vs present."""
        if end is None:
            return 0, 0
        first_close = (start.replace(second=0, microsecond=0) + timedelta(minutes=1))
        expected = max(0, int((end - first_close).total_seconds() // 60) + 1) if end >= first_close else 0
        present = self.conn.execute(
            "SELECT count(*) AS n FROM klines WHERE instrument_id = %s AND interval_s = 60"
            " AND open_time + interval '60 seconds' > %s AND open_time + interval '60 seconds' <= %s",
            [iid, start, end]).fetchone()["n"]
        return expected, present

    def _window_gap(self, iid: int, key: str, reason: str, start: datetime, end: Optional[datetime], details: dict):
        # book: per-second summaries cannot be reconstructed afterwards
        self._upsert_gap(iid, "book", key, reason, "open" if end is None else "unrecoverable", start, end,
                         details=details)

    def _kline_gap(self, iid: int, key: str, reason: str, start: datetime, end: Optional[datetime], details: dict):
        expected, present = self._kline_window(iid, start, end)
        if end is not None and expected == 0:
            return  # no candle closed while the stream was down
        status = ("open" if end is None else "recovered" if present >= expected
                  else "partial" if present else "unrecoverable")
        self._upsert_gap(iid, "kline", key, reason, status, start, end, expected=expected or None,
                         present=present, details=details)

    def _last_data_before(self, iid: int, ts: datetime, rest_ids: List[Tuple[int, int]]) -> Optional[datetime]:
        """Newest sign that the collector's WebSocket was receiving before ``ts``. Rows the collector
        fetched over REST afterwards (kline history/backfill, trade id backfill in ``rest_ids``) fill
        the downtime with old event times, so they are not evidence that it was up."""
        row = self.conn.execute(
            "SELECT greatest("
            " (SELECT max(captured_ts) FROM book_snapshots WHERE instrument_id = %(i)s AND captured_ts < %(t)s),"
            " (SELECT max(open_time) + interval '60 seconds' FROM klines WHERE instrument_id = %(i)s"
            "   AND interval_s = 60 AND open_time <= %(t)s - interval '60 seconds' AND source = 'exchange_kline'),"
            " (SELECT max(event_ts) FROM trades WHERE instrument_id = %(i)s AND event_ts < %(t)s"
            "   AND quality = 'ok' AND NOT EXISTS (SELECT 1 FROM unnest(%(lo)s::bigint[], %(hi)s::bigint[]) r(lo, hi)"
            "   WHERE agg_trade_id BETWEEN r.lo AND r.hi))) AS t",
            {"i": iid, "t": ts, "lo": [a for a, _ in rest_ids], "hi": [b for _, b in rest_ids]}).fetchone()
        return row["t"]

    def _tag_rest_klines(self, iid: int, metas: List[dict]) -> None:
        """kline_history / kline_backfill [ts, event, first_open_ms, last_open_ms, count]: the minutes
        the collector wrote from REST. Re-tagged on every run (rows may be loaded after their meta)."""
        for m in metas:
            p = m["payload"]
            if len(p) >= 4 and p[1] in ("kline_history", "kline_backfill"):
                try:
                    lo, hi = ms(p[2]), ms(p[3])
                except (TypeError, ValueError, ArithmeticError):
                    continue
                cur = self.conn.execute(
                    "UPDATE klines SET source = 'rest_backfill' WHERE instrument_id = %s AND interval_s = 60"
                    " AND open_time BETWEEN %s AND %s AND source = 'exchange_kline'", [iid, lo, hi])
                self.stats["klines_tagged_rest"] += cur.rowcount

    def derive_quality(self, iid: int) -> None:
        metas = self.conn.execute(
            "SELECT received_ts AS ts, payload FROM raw_events WHERE instrument_id = %s AND stream = 'meta'"
            " ORDER BY received_ts, event_id", [iid]).fetchall()
        self._tag_rest_klines(iid, metas)
        rest_ids = []          # trade id ranges fetched over REST: [ts, "backfill", lo, hi, recovered]
        for m in metas:
            p = m["payload"]
            if len(p) >= 4 and p[1] == "backfill":
                try:
                    rest_ids.append((int(p[2]), int(p[3])))
                except (TypeError, ValueError):
                    continue
        seen_connect = False
        disconnect: Optional[Tuple[datetime, str]] = None
        book_broken: Optional[Tuple[datetime, str, str]] = None
        for m in metas:
            ts, p = m["ts"], m["payload"]
            ev = p[1] if len(p) > 1 else None
            key_ms = int((ts - EPOCH).total_seconds() * 1000)
            if ev == "disconnected":
                if disconnect is None:
                    disconnect = (ts, str(p[2]) if len(p) > 2 else "")
                if book_broken is None:
                    book_broken = (ts, "disconnected", f"disconnect:{key_ms}")
            elif ev == "connected":
                if disconnect is not None:
                    d_ts, msg = disconnect
                    self._kline_gap(iid, f"disconnect:{int((d_ts - EPOCH).total_seconds() * 1000)}", "disconnected",
                                    d_ts, ts, {"message": msg[:200]})
                    disconnect = None
                elif seen_connect:
                    last = self._last_data_before(iid, ts, rest_ids)
                    if last is not None and ts - last > MIN_RESTART_GAP:
                        self._kline_gap(iid, f"restart:{key_ms}", "collector_restart", last, ts,
                                        {"note": "collector stopped without a disconnect record"})
                        if book_broken is None:
                            book_broken = (last, "collector_restart", f"restart:{key_ms}")
                seen_connect = True
            elif ev == "book_resync":
                if book_broken is None:
                    book_broken = (ts, "book_resync", f"resync:{key_ms}")
            elif ev == "book_synced":
                if book_broken is not None:
                    s_ts, reason, key = book_broken
                    self._window_gap(iid, key, reason, s_ts, ts, {"synced_update_id": p[2] if len(p) > 2 else None})
                    book_broken = None
            elif ev == "kline_gap" and len(p) >= 4:
                # minutes the collector could not get over REST [first_open_ms, last_open_ms, reason];
                # re-evaluated on every run, so a later history fetch that fills them flips it to recovered
                self._kline_gap(iid, f"kline_gap:{p[2]}-{p[3]}", "collector_kline_gap", ms(p[2]),
                                ms(p[3]) + timedelta(minutes=1), {"reason": str(p[4])[:200] if len(p) > 4 else ""})
            elif ev in ("backfill", "trade_gap") and len(p) >= 4:
                lo, hi = int(p[2]), int(p[3])
                status, expected, present, before, after = self._id_range_status(iid, lo, hi, final=True)
                details = {"collector_recovered": int(p[4])} if ev == "backfill" and len(p) > 4 else {}
                self._upsert_gap(iid, "trades", f"{ev}:{lo}-{hi}",
                                 "reconnect_backfill" if ev == "backfill" else "collector_lost_ids", status,
                                 before or ts, after or ts, lo, hi, expected, present, details)
        if disconnect is not None:
            d_ts, msg = disconnect
            self._kline_gap(iid, f"disconnect:{int((d_ts - EPOCH).total_seconds() * 1000)}", "disconnected",
                            d_ts, None, {"message": msg[:200]})
        if book_broken is not None:
            s_ts, reason, key = book_broken
            self._window_gap(iid, key, reason, s_ts, None, {})
        self._scan_trade_ids(iid)
        self._reevaluate_trade_gaps(iid)

    def _scan_trade_ids(self, iid: int) -> None:
        rng = self.id_ranges.get(iid)
        if not rng:
            return
        lo, hi = rng
        lo_b = self.conn.execute("SELECT max(agg_trade_id) AS v FROM trades WHERE instrument_id = %s AND agg_trade_id < %s",
                                 [iid, lo]).fetchone()["v"] or lo
        hi_b = self.conn.execute("SELECT min(agg_trade_id) AS v FROM trades WHERE instrument_id = %s AND agg_trade_id > %s",
                                 [iid, hi]).fetchone()["v"] or hi
        holes = self.conn.execute(
            "SELECT a + 1 AS lo, nxt - 1 AS hi FROM (SELECT agg_trade_id AS a, lead(agg_trade_id) OVER"
            " (ORDER BY agg_trade_id) AS nxt FROM trades WHERE instrument_id = %s AND agg_trade_id BETWEEN %s AND %s) s"
            " WHERE nxt - a > 1", [iid, lo_b, hi_b]).fetchall()
        for h in holes:
            covered = self.conn.execute(
                "SELECT 1 FROM data_quality WHERE instrument_id = %s AND stream = 'trades'"
                " AND first_missing_id <= %s AND last_missing_id >= %s LIMIT 1", [iid, h["lo"], h["hi"]]).fetchone()
            if covered:
                continue
            status, expected, present, before, after = self._id_range_status(iid, h["lo"], h["hi"], final=False)
            self._upsert_gap(iid, "trades", f"idgap:{h['lo']}-{h['hi']}", "id_gap_detected", status, before, after,
                             h["lo"], h["hi"], expected, present, {})

    def _reevaluate_trade_gaps(self, iid: int) -> None:
        rows = self.conn.execute(
            "SELECT id, first_missing_id AS lo, last_missing_id AS hi, reason FROM data_quality"
            " WHERE instrument_id = %s AND stream = 'trades' AND status <> 'recovered'"
            " AND first_missing_id IS NOT NULL", [iid]).fetchall()
        for r in rows:
            status, expected, present, _, _ = self._id_range_status(iid, r["lo"], r["hi"],
                                                                    final=r["reason"] != "id_gap_detected")
            self.conn.execute(
                "UPDATE data_quality SET status = %s, present_count = %s, expected_count = %s,"
                " repaired_at = CASE WHEN %s = 'recovered' THEN coalesce(repaired_at, now()) END WHERE id = %s",
                [status, present, expected, status, r["id"]])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="load collector files into PostgreSQL")
    ap.add_argument("--data", required=True, help="collector data_dir, e.g. ../data/live")
    ap.add_argument("--dsn", default=os.environ.get("FLOWLAB_INGEST_DATABASE_URL"),
                    help="ingestion role connection (default: $FLOWLAB_INGEST_DATABASE_URL)")
    ap.add_argument("--venue", default="binance")
    ap.add_argument("--collector-config", help="live/config.json, for book bucket size and window")
    ap.add_argument("--force", action="store_true", help="re-read files whose checksum did not change")
    args = ap.parse_args(argv)
    if not args.dsn:
        ap.error("set FLOWLAB_INGEST_DATABASE_URL or pass --dsn")
    book_cfg = {}
    if args.collector_config:
        with open(args.collector_config, encoding="utf-8") as fh:
            book_cfg = json.load(fh).get("book", {})
    with psycopg.connect(args.dsn, autocommit=True, row_factory=dict_row,
                         options="-c search_path=flowlab -c timezone=UTC") as conn:
        stats = Ingestor(conn, Path(args.data), args.venue, book_cfg, args.force).run()
    print(json.dumps(stats, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
