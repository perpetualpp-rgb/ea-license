#!/usr/bin/env python3
"""24/7 Binance market-data collector (phase 0 of ROADMAP.md).

    pip install -r live/requirements.txt
    python3 -m live.collector                          # live/config.json
    python3 -m live.collector --config live/config.vision.json --minutes 3

One WebSocket per market carries every symbol's streams. Rows are JSON arrays
written by ``live.sink.HourlySink`` under ``data_dir``:

    trades  [T, aggId, price, qty, takerSold]       every aggTrade (1 = taker sold)
    book    [ts, updateId, mid, [[p, q]...bids], [[p, q]...asks]]
                                                    local book bucketed within
                                                    ±window_pct of mid, every second
    kline   [t, o, h, l, c, v, trades, takerBuyV]   closed 1-minute candles
    mark    [E, mark, index, funding, nextFunding]  futures, every mark_every_s
    oi      [time, openInterest]                    futures, every oi_every_s
    liq     [T, side, price, avgPrice, qty, status] futures liquidation orders
    meta    [ts, event, ...]                         connects, resyncs, gaps, backfills

Trade ids are continuous per symbol; after a reconnect or restart the missing
range is fetched over REST so the trade record stays complete.
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
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(HERE)
if LAB not in sys.path:
    sys.path.insert(0, LAB)

from feeds.http import get_json  # noqa: E402
from live.book import LocalBook  # noqa: E402
from live.sink import HourlySink  # noqa: E402

log = logging.getLogger("collector")

STREAMS = {"spot": ["aggTrade", "depth@100ms", "kline_1m"],
           "futures": ["aggTrade", "depth@100ms", "kline_1m", "markPrice@1s", "forceOrder"]}
REST = {"spot": {"depth": "/api/v3/depth", "agg": "/api/v3/aggTrades"},
        "futures": {"depth": "/fapi/v1/depth", "agg": "/fapi/v1/aggTrades", "oi": "/fapi/v1/openInterest"}}
SNAP_LIMIT = {"spot": 5000, "futures": 1000}
BACKFILL_MAX = 300_000  # trades recovered per gap; anything beyond is logged as a gap


def now_ms() -> int:
    return int(time.time() * 1000)


class Blocked(Exception):
    """Binance answered 451: this machine's location is restricted."""


class Sym:
    def __init__(self, market: str, symbol: str, book_cfg: dict, last_agg: Optional[int]):
        self.market, self.symbol = market, symbol
        self.book = LocalBook(market)
        self.bucket = float(book_cfg.get("bucket", 1.0))
        self.window = float(book_cfg.get("window_pct", 1.0))
        self.last_agg = last_agg
        self.snap_busy = False
        self.gaps: List[Tuple[int, int]] = []
        self.backfilling = False
        self.mark: Optional[list] = None
        self.n = {"trades": 0, "backfilled": 0, "book": 0, "kline": 0, "liq": 0, "lost": 0}
        self.last_msg = 0

    @property
    def key(self) -> str:
        return f"{self.market}:{self.symbol}"


class Collector:
    def __init__(self, cfg: dict, root: str, state_path: str):
        self.cfg = cfg
        self.sink = HourlySink(root)
        self.root = root
        self.state_path = state_path
        state = self._load_state()
        self.syms: Dict[Tuple[str, str], Sym] = {}
        for market, mc in cfg["markets"].items():
            for s in mc["symbols"]:
                self.syms[(market, s)] = Sym(market, s, cfg.get("book", {}).get(s, {}), state.get(f"{market}:{s}"))
        self.stop = asyncio.Event()
        self.reconnects = {m: 0 for m in cfg["markets"]}
        self.connected = {m: False for m in cfg["markets"]}
        self.blocked: Dict[str, str] = {}
        self.started = time.time()

    # -- state --------------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                return json.load(fh).get("last_agg", {})
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        data = {"last_agg": {s.key: s.last_agg for s in self.syms.values() if s.last_agg is not None}, "saved": now_ms()}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.state_path)

    def meta(self, sym: Sym, event: str, *detail) -> None:
        self.sink.write(sym.market, sym.symbol, "meta", now_ms(), [now_ms(), event, *detail])
        log.info("%s %s %s", sym.key, event, " ".join(str(d) for d in detail))

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
        backoff = 1.0
        while not self.stop.is_set():
            try:
                async with connect(url, open_timeout=20, ping_interval=20, ping_timeout=20, max_size=2 ** 23, max_queue=4096) as ws:
                    self.connected[market] = True
                    backoff = 1.0
                    for s in syms:
                        s.book.reset()
                        self.meta(s, "connected")
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
                    self.blocked[market] = "Binance ปฏิเสธการเชื่อมต่อจากตำแหน่งนี้ (451)"
                for s in syms:
                    self.meta(s, "disconnected", msg[:200])
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)
        self.connected[market] = False

    def handle(self, market: str, msg: dict) -> None:
        d = msg.get("data", msg)
        sym = self.syms.get((market, d.get("s") or (d.get("o") or {}).get("s", "")))
        if sym is None:
            return
        sym.last_msg = now_ms()
        e = d.get("e")
        if e == "aggTrade":
            self.on_trade(sym, d)
        elif e == "depthUpdate":
            if not sym.book.on_event(d):
                self.meta(sym, "book_resync", sym.book.resyncs)
            if not sym.book.synced and not sym.snap_busy:
                sym.snap_busy = True
                asyncio.get_running_loop().create_task(self.snapshot(sym))
        elif e == "kline":
            k = d["k"]
            if k.get("x"):
                self.sink.write(market, sym.symbol, "kline", d["E"], [k["t"], float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]),
                                                                    float(k["v"]), int(k["n"]), float(k["V"])])
                sym.n["kline"] += 1
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
                sym.gaps.append((sym.last_agg + 1, a - 1))
                if not sym.backfilling:
                    sym.backfilling = True
                    asyncio.get_running_loop().create_task(self.backfill(sym))
        self.sink.write(sym.market, sym.symbol, "trades", d.get("E") or d["T"], [d["T"], a, float(d["p"]), float(d["q"]), 1 if d["m"] else 0])
        sym.last_agg = a
        sym.n["trades"] += 1

    async def backfill(self, sym: Sym) -> None:
        try:
            while sym.gaps and not self.stop.is_set():
                lo, hi = sym.gaps.pop(0)
                cur, got = lo, 0
                while cur <= hi and got < BACKFILL_MAX and not self.stop.is_set():
                    rows = await self.rest(sym.market, "agg", {"symbol": sym.symbol, "fromId": cur, "limit": 1000})
                    if not rows:
                        break
                    for r in rows:
                        a = int(r["a"])
                        if a < cur:
                            continue
                        if a > hi:
                            cur = hi + 1
                            break
                        self.sink.write(sym.market, sym.symbol, "trades", now_ms(), [r["T"], a, float(r["p"]), float(r["q"]), 1 if r["m"] else 0])
                        got += 1
                        cur = a + 1
                    await asyncio.sleep(0.25)
                sym.n["backfilled"] += got
                if cur <= hi:
                    sym.n["lost"] += hi - cur + 1
                    self.meta(sym, "trade_gap", cur, hi)
                self.meta(sym, "backfill", lo, hi, got)
        except Blocked:
            self.blocked[sym.market] = "Binance ปฏิเสธการเชื่อมต่อจากตำแหน่งนี้ (451)"
        except Exception as e:  # noqa: BLE001
            self.meta(sym, "backfill_error", str(e)[:200])
        finally:
            sym.backfilling = False

    async def snapshot(self, sym: Sym) -> None:
        try:
            for attempt in range(6):
                await asyncio.sleep(0.6 if attempt == 0 else 2.0)  # let a few diffs buffer first
                if sym.book.synced or self.stop.is_set():
                    return
                snap = await self.rest(sym.market, "depth", {"symbol": sym.symbol, "limit": SNAP_LIMIT[sym.market]})
                if sym.book.load_snapshot(snap):
                    self.meta(sym, "book_synced", int(snap["lastUpdateId"]))
                    return
            self.meta(sym, "book_sync_failed")
        except Blocked:
            self.blocked[sym.market] = "Binance ปฏิเสธการเชื่อมต่อจากตำแหน่งนี้ (451)"
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
            t = now_ms()
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
            await self._sleep(every - (time.time() * 1000 - t) / 1000)

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
                    self.blocked["futures"] = "Binance ปฏิเสธการเชื่อมต่อจากตำแหน่งนี้ (451)"
                except Exception as e:  # noqa: BLE001
                    log.warning("oi %s: %s", s.symbol, e)
            await self._sleep(every)

    async def status_loop(self) -> None:
        last_log = 0.0
        while not self.stop.is_set():
            self.write_status()
            self._save_state()
            if time.time() - last_log >= 60:
                last_log = time.time()
                for s in self.syms.values():
                    log.info("%s trades=%d backfilled=%d lost=%d book=%s rows=%d klines=%d liq=%d",
                             s.key, s.n["trades"], s.n["backfilled"], s.n["lost"], "ok" if s.book.synced else "sync…",
                             s.n["book"], s.n["kline"], s.n["liq"])
                for m, why in self.blocked.items():
                    log.error("%s: %s", m, why)
            await self._sleep(10)

    def write_status(self) -> None:
        t = now_ms()
        status = {"updated": t, "uptime_s": int(time.time() - self.started), "rows_written": self.sink.rows,
                  "connected": self.connected, "reconnects": self.reconnects, "blocked": self.blocked,
                  "symbols": {s.key: {"last_msg_age_s": round((t - s.last_msg) / 1000, 1) if s.last_msg else None,
                                      "book_synced": s.book.synced, "book_resyncs": s.book.resyncs,
                                      "last_agg_id": s.last_agg, "pending_gaps": len(s.gaps), **s.n}
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
        if minutes:
            loop.call_later(minutes * 60, self.stop.set)
        await self.stop.wait()
        await asyncio.sleep(0.5)
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
