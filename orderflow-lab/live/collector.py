#!/usr/bin/env python3
"""24/7 Binance market-data collector (phase 0 of ROADMAP.md).

    pip install -r live/requirements.txt
    python3 -m live.collector                          # live/config.json
    python3 -m live.collector --config live/config.vision.json --minutes 3

One WebSocket per market carries every symbol's streams. Rows are JSON arrays
written by ``live.sink.HourlySink`` under ``data_dir``:

    trades  [T, aggId, price, qty, takerSold]       every aggTrade (1 = taker sold)
    book    [ts, updateId, mid, [[p, q]...bids], [[p, q]...asks]]
                                                    SAMPLED SUMMARY: the local book
                                                    bucketed within ±window_pct of
                                                    mid, once a second
    kline   [t, o, h, l, c, v, trades, takerBuyV]   closed 1-minute candles (WebSocket, and REST
                                                    history/backfill, see below)
    mark    [E, mark, index, funding, nextFunding]  futures, every mark_every_s
    oi      [time, openInterest]                    futures, every oi_every_s
    liq     [T, side, price, avgPrice, qty, status] futures liquidation orders
    meta    [ts, event, ...]                         connects, resyncs, gaps, backfills (trades and klines)
    depth   [recv, E, T, U, u, pu, bids, asks]      only with "store_depth_diffs": every
                                                    raw diff event (prices/sizes as sent)
    depth_snap [recv, lastUpdateId, bids, asks]     only with "store_depth_diffs": each
                                                    REST snapshot the book was synced from

The ``book`` stream is a 1 s sample of price buckets: it cannot show what was
added or cancelled between samples, queue position or single orders. Replaying
the book at full (100 ms) resolution needs ``store_depth_diffs`` (off by
default: several GB a day for BTC), which keeps the diffs with their update ids
and the snapshots they chain from.

Trade ids are continuous per symbol. A missing range goes into a gap ledger
(saved in state.json with the last trade id) and is fetched over REST; a
failed request is retried with exponential backoff and the ledger survives a
restart. Ids that cannot be fetched (beyond BACKFILL_MAX, or absent from the
exchange's answer) are recorded as unrecoverable, in the ledger and as a
``trade_gap`` meta row, never skipped silently.

1-minute klines also come over REST (``/api/v3/klines`` pages of up to 1000,
``/fapi/v1/klines`` pages of up to 1500, from the market's ``rest`` base):

* at start, per symbol, the newest ``kline_history_min`` (default 1440) closed
  candles, so a fresh database has a day of history;
* after every (re)connect, the candles between the last one written and the
  first one the socket sends (the minutes the socket was down).

They go to the same ``kline`` stream in the same row format, filed by the
collector clock like backfilled trades, and only closed candles (close time
before the clock) are written. A minute written once in this run, or already on
disk inside the history window, is never written again. Each contiguous run of
written minutes gets a meta row ``kline_history`` (start) or ``kline_backfill``
(reconnect) ``[first_open_ms, last_open_ms, count]``; ``live.check`` uses them
to keep REST candles out of the WebSocket coverage. A failed page is retried
with the trade backfill's backoff (``kline_retry`` rows); minutes that still
cannot be had (``KLINE_ATTEMPTS`` failures, 451/403, stop, absent from the
answer, beyond ``KLINE_BACKFILL_MAX``) get ``kline_gap [first_open_ms,
last_open_ms, reason]``, never dropped silently. The next start's history fetch
fills a kline gap that is still inside its window.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
import urllib.error
from typing import Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(HERE)
if LAB not in sys.path:
    sys.path.insert(0, LAB)

from feeds.http import get_json  # noqa: E402
from live.book import LocalBook  # noqa: E402
from live.sink import HourlySink, read_rows, stream_files  # noqa: E402

log = logging.getLogger("collector")

STREAMS = {"spot": ["aggTrade", "depth@100ms", "kline_1m"],
           "futures": ["aggTrade", "depth@100ms", "kline_1m", "markPrice@1s", "forceOrder"]}
REST = {"spot": {"depth": "/api/v3/depth", "agg": "/api/v3/aggTrades", "klines": "/api/v3/klines"},
        "futures": {"depth": "/fapi/v1/depth", "agg": "/fapi/v1/aggTrades", "oi": "/fapi/v1/openInterest",
                    "klines": "/fapi/v1/klines"}}
SNAP_LIMIT = {"spot": 5000, "futures": 1000}
BACKFILL_MAX = 300_000  # trades recovered per gap; ids beyond are recorded as unrecoverable
AGG_PAGE = 1000
RETRY_BASE_S, RETRY_CAP_S = 1.0, 300.0  # backfill retry delay: base * 2^(attempts-1), capped
KEEP_UNRECOVERABLE = 50  # ledger entries kept per symbol for the record (the meta stream has all of them)
BLOCKED = "Binance ปฏิเสธการเชื่อมต่อจากตำแหน่งนี้ (451)"
BLOCKED_403 = "Binance ปฏิเสธคำขอจากตำแหน่งนี้ (403)"
MIN_MS, DAY_MS = 60_000, 86_400_000
KLINE_PAGE = {"spot": 1000, "futures": 1500}  # the largest limit /api/v3/klines and /fapi/v1/klines accept
KLINE_HISTORY_MIN = 1440  # default "kline_history_min": closed 1m candles fetched per symbol at start
KLINE_BACKFILL_MAX = 10_080  # minutes fetched for one reconnect span; older minutes are recorded as kline_gap
KLINE_ATTEMPTS = 6  # failed REST pages per span before the rest of the span is recorded as kline_gap


def now_ms() -> int:
    return int(time.time() * 1000)


def minute_runs(opens: List[int]) -> List[Tuple[int, int]]:
    """Sorted open times (ms) -> [(first, last)] of consecutive minutes."""
    out: List[Tuple[int, int]] = []
    for t in opens:
        if out and t == out[-1][1] + MIN_MS:
            out[-1] = (out[-1][0], t)
        else:
            out.append((t, t))
    return out


class Blocked(Exception):
    """Binance answered 451 (this machine's location is restricted), or 403 to a kline request."""

    def __init__(self, what: str = "", code: int = 451):
        super().__init__(what)
        self.code = code


class Sym:
    def __init__(self, market: str, symbol: str, book_cfg: dict, last_agg: Optional[int], gaps: Optional[list] = None):
        self.market, self.symbol = market, symbol
        self.book = LocalBook(market)
        self.bucket = float(book_cfg.get("bucket", 1.0))
        self.window = float(book_cfg.get("window_pct", 1.0))
        self.last_agg = last_agg
        self.snap_busy = False
        # gap ledger: {lo, hi, next, attempts, status: pending|partial|unrecoverable, last_error, got}
        self.gaps: List[dict] = [dict(g) for g in gaps or []]
        self.backfilling = False
        # 1m klines: open times written in this run (plus those found on disk inside the history window),
        # spans recorded as kline_gap, and the REST jobs (history, reconnect spans), worked one at a time
        self.klines: set = set()
        self.kline_void: List[Tuple[int, int]] = []
        self.kline_jobs: List[dict] = []
        self.kline_busy = False
        self.kline_resume = False  # set on (re)connect: the next kline event measures what the socket missed
        self.mark: Optional[list] = None
        # kline = written from the WebSocket, kline_rest = written from REST, kline_missing = minutes in kline_gap rows
        self.n = {"trades": 0, "backfilled": 0, "book": 0, "kline": 0, "kline_rest": 0, "kline_missing": 0,
                  "liq": 0, "lost": 0}
        self.last_msg = 0

    @property
    def key(self) -> str:
        return f"{self.market}:{self.symbol}"


class Collector:
    def __init__(self, cfg: dict, root: str, state_path: str, clock: Optional[Callable[[], int]] = None):
        self.cfg = cfg
        self.clock = clock or now_ms  # every timestamp the collector makes comes from here (tests freeze it)
        self.sink = HourlySink(root, clock=self.clock)
        self.root = root
        self.state_path = state_path
        state = self._load_state()
        last, ledger = state.get("last_agg", {}), state.get("gaps", {})
        self.syms: Dict[Tuple[str, str], Sym] = {}
        for market, mc in cfg["markets"].items():
            for s in mc["symbols"]:
                key = f"{market}:{s}"
                self.syms[(market, s)] = Sym(market, s, cfg.get("book", {}).get(s, {}), last.get(key), ledger.get(key))
        self.store_depth = bool(cfg.get("store_depth_diffs", False))
        self.stop = asyncio.Event()
        self.reconnects = {m: 0 for m in cfg["markets"]}
        self.connected = {m: False for m in cfg["markets"]}
        self.blocked: Dict[str, str] = {}
        self.started = self.clock()
        self.retry_base_s, self.retry_cap_s, self.page_pause_s = RETRY_BASE_S, RETRY_CAP_S, 0.25
        self.reconnect_base_s = 1.0  # first WebSocket reconnect delay (doubles to 60 s)
        self.kline_history_min = max(0, int(cfg.get("kline_history_min", KLINE_HISTORY_MIN)))
        self.kline_attempts = KLINE_ATTEMPTS
        self._tasks: set = set()

    # -- state --------------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        # checkpoint only what is on disk: the last id and ledger progress must not run ahead of the rows
        self.sink.sync()
        data = {"last_agg": {s.key: s.last_agg for s in self.syms.values() if s.last_agg is not None},
                "gaps": {s.key: s.gaps for s in self.syms.values() if s.gaps}, "saved": self.clock()}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.state_path)

    def meta(self, sym: Sym, event: str, *detail) -> None:
        t = self.clock()
        self.sink.write(sym.market, sym.symbol, "meta", t, [t, event, *detail])
        log.info("%s %s %s", sym.key, event, " ".join(str(d) for d in detail))

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)  # keep a reference so the task is not garbage-collected mid-run
        task.add_done_callback(self._tasks.discard)

    # -- REST ---------------------------------------------------------------------
    async def rest(self, market: str, name: str, params: dict):
        url = self.cfg["markets"][market]["rest"] + REST[market][name]
        try:
            return await asyncio.to_thread(get_json, url, params, 15.0)
        except urllib.error.HTTPError as e:
            if e.code == 451:
                raise Blocked(url) from e
            if e.code in (418, 429):
                log.warning("%s rate limited (%s); pausing 60 s", market, e.code)
                await asyncio.sleep(60)
            raise

    # -- WebSocket loop -----------------------------------------------------------
    async def market_loop(self, market: str) -> None:
        from websockets.asyncio.client import connect  # imported here so tests run without the package

        mc = self.cfg["markets"][market]
        syms = [s for s in self.syms.values() if s.market == market]
        url = mc["ws"] + "?streams=" + "/".join(f"{s.symbol.lower()}@{x}" for s in syms for x in STREAMS[market])
        backoff = self.reconnect_base_s
        while not self.stop.is_set():
            try:
                async with connect(url, open_timeout=20, ping_interval=20, ping_timeout=20, max_size=2 ** 23, max_queue=4096) as ws:
                    self.connected[market] = True
                    backoff = self.reconnect_base_s
                    self.on_connect(market)
                    while not self.stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            raise ConnectionError("no data for 30 s")
                        self.handle(market, json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any failure means reconnect
                self.connected[market] = False
                if self.stop.is_set():
                    break
                self.reconnects[market] += 1
                msg = str(e) or type(e).__name__
                if "451" in msg:
                    self.blocked[market] = BLOCKED
                for s in syms:
                    self.meta(s, "disconnected", msg[:200])
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)
        self.connected[market] = False

    def on_connect(self, market: str) -> None:
        for s in self.syms.values():
            if s.market == market:
                s.book.reset()
                s.kline_resume = True
                self.meta(s, "connected")

    def handle(self, market: str, msg: dict) -> None:
        d = msg.get("data", msg)
        sym = self.syms.get((market, d.get("s") or (d.get("o") or {}).get("s", "")))
        if sym is None:
            return
        sym.last_msg = self.clock()
        e = d.get("e")
        if e == "aggTrade":
            self.on_trade(sym, d)
        elif e == "depthUpdate":
            if self.store_depth:
                self.sink.write(market, sym.symbol, "depth", d.get("E") or sym.last_msg,
                                [sym.last_msg, d.get("E"), d.get("T"), d["U"], d["u"], d.get("pu"), d.get("b", []), d.get("a", [])])
            if not sym.book.on_event(d):
                self.meta(sym, "book_resync", sym.book.resyncs)
            if not sym.book.synced and not sym.snap_busy:
                sym.snap_busy = True
                self._spawn(self.snapshot(sym))
        elif e == "kline":
            self.on_kline(sym, d)
        elif e == "markPriceUpdate":
            sym.mark = [d["E"], float(d["p"]), float(d.get("i") or 0), float(d.get("r") or 0), int(d.get("T") or 0)]
        elif e == "forceOrder":
            o = d["o"]
            self.sink.write(market, sym.symbol, "liq", d["E"], [o["T"], o["S"], float(o["p"]), float(o["ap"]), float(o["q"]), o["X"]])
            sym.n["liq"] += 1

    def on_trade(self, sym: Sym, d: dict) -> None:
        a = int(d["a"])
        if sym.last_agg is not None:
            if a <= sym.last_agg:
                return  # duplicate after a reconnect
            if a > sym.last_agg + 1:
                self.add_gap(sym, sym.last_agg + 1, a - 1)
        self.sink.write(sym.market, sym.symbol, "trades", d.get("E") or d["T"], [d["T"], a, float(d["p"]), float(d["q"]), 1 if d["m"] else 0])
        sym.last_agg = a
        sym.n["trades"] += 1

    def on_kline(self, sym: Sym, d: dict) -> None:
        k = d["k"]
        t = int(k["t"])
        if sym.kline_resume:
            # first kline event after a (re)connect, closed or not: every candle before it has closed,
            # and those the socket did not deliver are fetched over REST
            sym.kline_resume = False
            self.queue_klines(sym, {"kind": "reconnect", "until": t})
        if not k.get("x") or t in sym.klines:
            return  # still open, or already written in this run (REST history)
        self.sink.write(sym.market, sym.symbol, "kline", d["E"], [t, float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]),
                                                                float(k["v"]), int(k["n"]), float(k["V"])])
        self._kline_written(sym, t)
        sym.n["kline"] += 1

    # -- 1m klines over REST: history at start, the disconnected span after a reconnect ----
    def _kline_written(self, sym: Sym, t: int) -> None:
        sym.klines.add(t)
        keep = self.kline_history_min + 1440  # minutes kept for dedupe (pruned against the newest one)
        if len(sym.klines) > 2 * keep:
            floor = max(sym.klines) - keep * MIN_MS
            sym.klines = {x for x in sym.klines if x >= floor}

    def kline_before(self, sym: Sym, t: int) -> Optional[int]:
        """Newest minute before ``t`` that is written (or on disk) or already recorded as a kline_gap."""
        best = max((x for x in sym.klines if x < t), default=None)
        for a, b in sym.kline_void:
            if a < t:
                b = min(b, t - MIN_MS)
                best = b if best is None else max(best, b)
        return best

    def _kline_void(self, sym: Sym, a: int, b: int, why: str) -> None:
        sym.kline_void.append((a, b))
        del sym.kline_void[:-KEEP_UNRECOVERABLE]
        sym.n["kline_missing"] += (b - a) // MIN_MS + 1
        self.meta(sym, "kline_gap", a, b, why)

    def _seed_klines(self, sym: Sym, lo: int, hi: int) -> None:
        """Minutes lo..hi an earlier run already wrote (rows sit in the files of their day or later)."""
        for day in range(lo // DAY_MS, self.clock() // DAY_MS + 1):
            for path in stream_files(self.root, sym.market, sym.symbol, "kline", time.strftime("%Y-%m-%d", time.gmtime(day * 86400))):
                for r in read_rows(path):
                    if isinstance(r, list) and r and isinstance(r[0], int) and lo <= r[0] <= hi:
                        sym.klines.add(r[0])

    def start_kline_history(self) -> None:
        """At start: queue, per symbol, the newest ``kline_history_min`` closed 1m candles."""
        n = self.kline_history_min
        if n <= 0:
            return
        hi = self.clock() // MIN_MS * MIN_MS - MIN_MS  # newest candle whose close (open + 59 999 ms) is before now
        lo = hi - (n - 1) * MIN_MS
        for s in self.syms.values():
            self._seed_klines(s, lo, hi)
            self.queue_klines(s, {"kind": "history", "lo": lo, "hi": hi})

    def queue_klines(self, sym: Sym, job: dict) -> None:
        sym.kline_jobs.append(job)
        if not sym.kline_busy:
            sym.kline_busy = True
            self._spawn(self.kline_worker(sym))

    def _kline_span(self, sym: Sym, job: dict) -> Optional[Tuple[int, int, str]]:
        if job["kind"] == "history":
            return job["lo"], job["hi"], "kline_history"
        # reconnect: measured when the job runs, after the history and earlier spans have landed
        until = job["until"]
        before = self.kline_before(sym, until)
        if before is None:
            return None  # nothing written yet to measure from (history off, or its failure is already recorded)
        lo, hi = before + MIN_MS, until - MIN_MS
        if lo > hi:
            return None
        if (hi - lo) // MIN_MS + 1 > KLINE_BACKFILL_MAX:
            cut = hi - (KLINE_BACKFILL_MAX - 1) * MIN_MS
            self._kline_void(sym, lo, cut - MIN_MS, "beyond KLINE_BACKFILL_MAX")
            lo = cut
        return lo, hi, "kline_backfill"

    async def kline_worker(self, sym: Sym) -> None:
        """Work the symbol's kline jobs in order, so a reconnect span is measured after the history."""
        try:
            while sym.kline_jobs:
                span = self._kline_span(sym, sym.kline_jobs.pop(0))
                if span:
                    await self.fetch_klines(sym, *span)
        finally:
            sym.kline_busy = False
            while sym.kline_jobs:  # cancelled at shutdown: what is still queued is recorded, not dropped
                span = self._kline_span(sym, sym.kline_jobs.pop(0))
                if span:
                    self._kline_void(sym, span[0], span[1], "stopped before fetched")

    async def _rest_klines(self, sym: Sym, start: int, hi: int, limit: int) -> list:
        try:
            rows = await self.rest(sym.market, "klines", {"symbol": sym.symbol, "interval": "1m", "startTime": start,
                                                          "endTime": hi + MIN_MS - 1, "limit": limit})
        except urllib.error.HTTPError as e:
            if e.code in (403, 451):
                raise Blocked(str(e), e.code) from e
            raise
        if not isinstance(rows, list):
            raise ValueError(f"unexpected klines answer: {str(rows)[:100]}")
        return rows

    async def fetch_klines(self, sym: Sym, lo: int, hi: int, event: str) -> None:
        """Fetch the 1m candles opening lo..hi (ms) page by page and write those not written yet.
        Written minutes get an ``event`` meta row per consecutive run; minutes still missing at
        the end get ``kline_gap`` rows with the reason."""
        nxt, attempts, why = lo, 0, None
        wrote: List[int] = []
        unclosed: set = set()
        try:
            ahead = hi + MIN_MS - self.clock()  # > 0: the exchange clock runs ahead of ours, hi has not closed here yet
            if 0 < ahead <= 5000:
                await self._sleep(ahead / 1000 + 0.2)
            while nxt <= hi:
                if self.stop.is_set():
                    why = "stopped before fetched"
                    break
                limit = min(KLINE_PAGE[sym.market], (hi - nxt) // MIN_MS + 1)
                try:
                    rows = await self._rest_klines(sym, nxt, hi, limit)
                    newest = self._take_klines(sym, rows, nxt, hi, wrote, unclosed)
                except Blocked:
                    raise
                except Exception as e:  # noqa: BLE001 - network, HTTP or an unusable answer: retry after a backoff
                    attempts += 1
                    why = (str(e) or type(e).__name__)[:200]
                    self.meta(sym, "kline_retry", lo, hi, nxt, attempts, why)
                    if attempts >= self.kline_attempts:
                        why = f"REST failed {attempts} times: {why}"
                        break
                    await self._sleep(self.retry_delay(attempts))
                    continue
                why = None
                # a short page means the exchange sent all it has up to hi
                nxt = hi + MIN_MS if newest is None or len(rows) < limit else newest + MIN_MS
                if self.page_pause_s and nxt <= hi:
                    await asyncio.sleep(self.page_pause_s)
        except Blocked as e:
            self.blocked[sym.market] = BLOCKED if e.code == 451 else BLOCKED_403
            why = f"blocked ({e.code})"
        except asyncio.CancelledError:
            why = "stopped before fetched"
            raise
        finally:
            for a, b in minute_runs(sorted(wrote)):
                self.meta(sym, event, a, b, (b - a) // MIN_MS + 1)
            missing = [t for t in range(lo, hi + 1, MIN_MS) if t not in sym.klines]
            for reason, ts in (("not closed by the collector clock", [t for t in missing if t in unclosed]),
                               ("missing from exchange answer", [t for t in missing if t < nxt and t not in unclosed]),
                               (why or "missing from exchange answer", [t for t in missing if t >= nxt and t not in unclosed])):
                for a, b in minute_runs(ts):
                    self._kline_void(sym, a, b, reason)

    def _take_klines(self, sym: Sym, rows: list, nxt: int, hi: int, wrote: List[int], unclosed: set) -> Optional[int]:
        """Write the closed candles of one REST page; returns the newest open time in nxt..hi it held."""
        now, newest = self.clock(), None
        for r in rows:
            t = int(r[0])
            if t < nxt or t > hi:
                continue
            newest = t if newest is None else max(newest, t)
            if int(r[6]) >= now:
                unclosed.add(t)  # close time not before the clock: never written as a closed candle
                continue
            if t in sym.klines:
                continue  # written in this run by the socket, or on disk from an earlier run
            row = [t, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), int(r[8]), float(r[9])]
            self.sink.write(sym.market, sym.symbol, "kline", now, row)
            self._kline_written(sym, t)
            unclosed.discard(t)
            wrote.append(t)
            sym.n["kline_rest"] += 1
        return newest

    # -- trade-id gaps: ledger, REST backfill, retry ----------------------------------
    def add_gap(self, sym: Sym, lo: int, hi: int) -> None:
        if hi - lo + 1 > BACKFILL_MAX:
            self.mark_unrecoverable(sym, lo + BACKFILL_MAX, hi, "beyond BACKFILL_MAX")
            hi = lo + BACKFILL_MAX - 1
        sym.gaps.append({"lo": lo, "hi": hi, "next": lo, "attempts": 0, "status": "pending", "last_error": None, "got": 0})
        self.start_backfill(sym)

    def mark_unrecoverable(self, sym: Sym, lo: int, hi: int, why: str) -> None:
        sym.n["lost"] += hi - lo + 1
        sym.gaps.append({"lo": lo, "hi": hi, "next": lo, "attempts": 0, "status": "unrecoverable", "last_error": why, "got": 0})
        dead = [g for g in sym.gaps if g["status"] == "unrecoverable"]
        for g in dead[:-KEEP_UNRECOVERABLE]:
            sym.gaps.remove(g)
        self.meta(sym, "trade_gap", lo, hi, why)

    def start_backfill(self, sym: Sym) -> None:
        if not sym.backfilling and any(g["status"] != "unrecoverable" for g in sym.gaps):
            sym.backfilling = True
            self._spawn(self.backfill(sym))

    def resume_backfills(self) -> None:
        """Pick up ranges left in the ledger by an earlier run."""
        for s in self.syms.values():
            self.start_backfill(s)

    def retry_delay(self, attempts: int) -> float:
        return min(self.retry_cap_s, self.retry_base_s * 2 ** max(0, attempts - 1))

    async def backfill(self, sym: Sym) -> None:
        """Work the ledger until every recoverable range is fetched; a failed range is
        retried after a backoff (other ranges go first) and never dropped."""
        gap = None
        try:
            while not self.stop.is_set():
                gap = next((g for g in sym.gaps if g["status"] != "unrecoverable"), None)
                if gap is None:
                    return
                try:
                    await self._fill(sym, gap)
                except Blocked:
                    raise
                except Exception as e:  # noqa: BLE001 - network, HTTP or an unusable answer: retry later
                    gap["attempts"] += 1
                    gap["last_error"] = (str(e) or type(e).__name__)[:200]
                    self.meta(sym, "backfill_retry", gap["lo"], gap["hi"], gap["next"], gap["attempts"], gap["last_error"])
                    sym.gaps.remove(gap)
                    sym.gaps.append(gap)
                    await self._sleep(self.retry_delay(gap["attempts"]))
        except Blocked:
            self.blocked[sym.market] = BLOCKED
            if gap is not None:
                gap["last_error"] = "451"  # stays in the ledger; the next start tries again
        finally:
            sym.backfilling = False

    async def _fill(self, sym: Sym, gap: dict) -> None:
        while gap["next"] <= gap["hi"] and not self.stop.is_set():
            rows = await self.rest(sym.market, "agg", {"symbol": sym.symbol, "fromId": gap["next"], "limit": AGG_PAGE})
            if not rows:
                raise LookupError(f"empty answer from id {gap['next']}")
            before = gap["next"]
            self._take_page(sym, gap, rows)
            if gap["next"] == before:
                raise LookupError(f"no ids at or after {before} in the answer")
            if self.page_pause_s:
                await asyncio.sleep(self.page_pause_s)
        if gap["next"] > gap["hi"]:
            sym.gaps.remove(gap)
            self.meta(sym, "backfill", gap["lo"], gap["hi"], gap["got"])

    def _take_page(self, sym: Sym, gap: dict, rows: list) -> None:
        for r in rows:
            if gap["next"] > gap["hi"]:
                break
            a = int(r["a"])
            if a < gap["next"]:
                continue
            if a > gap["hi"]:
                # aggTrade ids are consecutive; ids the exchange skipped cannot be fetched
                self.mark_unrecoverable(sym, gap["next"], gap["hi"], "missing from exchange answer")
                gap["next"] = gap["hi"] + 1
                break
            if a > gap["next"]:
                self.mark_unrecoverable(sym, gap["next"], a - 1, "missing from exchange answer")
            self.sink.write(sym.market, sym.symbol, "trades", self.clock(), [r["T"], a, float(r["p"]), float(r["q"]), 1 if r["m"] else 0])
            gap["got"] += 1
            sym.n["backfilled"] += 1
            gap["next"] = a + 1
        gap["status"] = "partial" if gap["next"] > gap["lo"] else "pending"

    async def snapshot(self, sym: Sym) -> None:
        try:
            for attempt in range(6):
                await asyncio.sleep(0.6 if attempt == 0 else 2.0)  # let a few diffs buffer first
                if sym.book.synced or self.stop.is_set():
                    return
                snap = await self.rest(sym.market, "depth", {"symbol": sym.symbol, "limit": SNAP_LIMIT[sym.market]})
                if sym.book.load_snapshot(snap):
                    if self.store_depth:
                        t = self.clock()
                        self.sink.write(sym.market, sym.symbol, "depth_snap", t, [t, int(snap["lastUpdateId"]), snap["bids"], snap["asks"]])
                    self.meta(sym, "book_synced", int(snap["lastUpdateId"]))
                    return
            self.meta(sym, "book_sync_failed")
        except Blocked:
            self.blocked[sym.market] = BLOCKED
        except Exception as e:  # noqa: BLE001
            self.meta(sym, "snapshot_error", str(e)[:200])
        finally:
            sym.snap_busy = False

    # -- periodic tasks -----------------------------------------------------------
    async def sampler(self) -> None:
        every = float(self.cfg.get("book_every_s", 1))
        mark_every = float(self.cfg.get("mark_every_s", 5)) * 1000
        last_mark: Dict[str, int] = {}
        while not self.stop.is_set():
            t, started = self.clock(), time.monotonic()
            for s in self.syms.values():
                if s.book.synced:
                    b = s.book.buckets(s.bucket, s.window)
                    if b:
                        mid, bids, asks = b
                        self.sink.write(s.market, s.symbol, "book", t, [t, s.book.last_id, round(mid, 8), bids, asks])
                        s.n["book"] += 1
                if s.mark and t - last_mark.get(s.key, 0) >= mark_every:
                    self.sink.write(s.market, s.symbol, "mark", s.mark[0], s.mark)
                    last_mark[s.key] = t
            self.sink.flush()
            await self._sleep(every - (time.monotonic() - started))

    async def oi_loop(self) -> None:
        every = float(self.cfg.get("oi_every_s", 60))
        while not self.stop.is_set():
            for s in self.syms.values():
                if s.market != "futures" or "futures" in self.blocked:
                    continue
                try:
                    r = await self.rest("futures", "oi", {"symbol": s.symbol})
                    self.sink.write("futures", s.symbol, "oi", int(r["time"]), [int(r["time"]), float(r["openInterest"])])
                except Blocked:
                    self.blocked["futures"] = BLOCKED
                except Exception as e:  # noqa: BLE001
                    log.warning("oi %s: %s", s.symbol, e)
            await self._sleep(every)

    async def status_loop(self) -> None:
        last_log = -60.0
        while not self.stop.is_set():
            self.write_status()
            self._save_state()
            if time.monotonic() - last_log >= 60:
                last_log = time.monotonic()
                for s in self.syms.values():
                    log.info("%s trades=%d backfilled=%d lost=%d book=%s rows=%d klines=%d kline_rest=%d kline_missing=%d liq=%d",
                             s.key, s.n["trades"], s.n["backfilled"], s.n["lost"], "ok" if s.book.synced else "sync…",
                             s.n["book"], s.n["kline"], s.n["kline_rest"], s.n["kline_missing"], s.n["liq"])
                for m, why in self.blocked.items():
                    log.error("%s: %s", m, why)
            await self._sleep(10)

    def write_status(self) -> None:
        t = self.clock()
        status = {"updated": t, "uptime_s": int((t - self.started) / 1000), "rows_written": self.sink.rows,
                  "connected": self.connected, "reconnects": self.reconnects, "blocked": self.blocked,
                  "symbols": {s.key: {"last_msg_age_s": round((t - s.last_msg) / 1000, 1) if s.last_msg else None,
                                      "book_synced": s.book.synced, "book_resyncs": s.book.resyncs,
                                      "last_agg_id": s.last_agg,
                                      "pending_gaps": sum(g["status"] != "unrecoverable" for g in s.gaps),
                                      "gaps": s.gaps, "kline_jobs": len(s.kline_jobs) + s.kline_busy, **s.n}
                              for s in self.syms.values()}}
        os.makedirs(self.root, exist_ok=True)
        tmp = os.path.join(self.root, "status.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(status, fh, indent=1)
        os.replace(tmp, os.path.join(self.root, "status.json"))

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=max(0.0, seconds))
        except asyncio.TimeoutError:
            pass

    # -- run ------------------------------------------------------------------------
    async def run(self, minutes: Optional[float] = None) -> None:
        n = self.sink.recover()
        if n:
            log.info("compressed %d hour file(s) left from an earlier run", n)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop.set)
            except (NotImplementedError, RuntimeError):
                pass  # Windows: Ctrl-C raises KeyboardInterrupt instead
        tasks = [asyncio.create_task(self.market_loop(m)) for m in self.cfg["markets"]]
        tasks += [asyncio.create_task(c) for c in (self.sampler(), self.oi_loop(), self.status_loop())]
        self.resume_backfills()
        self.start_kline_history()  # queued before any socket connects, so reconnect spans are measured after it
        if minutes:
            loop.call_later(minutes * 60, self.stop.set)
        await self.stop.wait()
        await asyncio.sleep(0.5)
        tasks += list(self._tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.write_status()
        self._save_state()
        self.sink.close()
        log.info("stopped; %d rows written", self.sink.rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--data", help="output folder (default: data_dir in the config, relative to orderflow-lab/)")
    ap.add_argument("--minutes", type=float, help="stop after this many minutes (for a test run)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    root = args.data or os.path.join(LAB, cfg.get("data_dir", "data/live"))
    os.makedirs(root, exist_ok=True)
    col = Collector(cfg, root, os.path.join(root, "state.json"))
    try:
        asyncio.run(col.run(args.minutes))
    except KeyboardInterrupt:
        col.sink.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
