"""Phase 0 collector: order book sync, hourly storage and message handling.

Every Collector here runs on a frozen clock, and rows are read back across all
hour/day partitions of a stream, so no test depends on the wall-clock time or
on which hour file a row landed in."""
import asyncio
import glob
import gzip
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
import urllib.error
from collections import Counter
from pathlib import Path
from unittest import mock

import live.collector as collector_mod
from live.book import LocalBook
from live.check import coverage, day_start
from live.collector import Collector
from live.sink import HourlySink, compress, hour_path, read_manifest, read_rows, stream_files

H = 3600 * 1000
T0 = 1792300000000  # 2026-10-18 05:06:40 UTC


class FrozenClock:
    """Injectable collector clock (ms); moves only when a test says so."""

    def __init__(self, t: int):
        self.t = t

    def __call__(self) -> int:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms


def stream_rows(root, market, symbol, stream):
    """Every row of one stream, whatever hour or day partition it was filed under."""
    pattern = os.path.join(root, market, symbol, stream, "*", "*.jsonl*")
    rows = []
    for path in sorted(p for p in glob.glob(pattern) if p.endswith((".jsonl", ".jsonl.gz"))):
        rows.extend(read_rows(path))
    return rows


def fut(U, u, pu, b=(), a=()):
    return {"e": "depthUpdate", "U": U, "u": u, "pu": pu, "b": [list(x) for x in b], "a": [list(x) for x in a]}


def spot(U, u, b=(), a=()):
    return {"e": "depthUpdate", "U": U, "u": u, "b": [list(x) for x in b], "a": [list(x) for x in a]}


SNAP = {"lastUpdateId": 100, "bids": [["99.0", "1"], ["98.5", "2"]], "asks": [["100.0", "1"], ["101.0", "3"]]}


class FuturesBook(unittest.TestCase):
    def test_buffer_bridge_and_chain(self):
        b = LocalBook("futures")
        b.on_event(fut(90, 95, 89, b=[("97", "5")]))           # older than the snapshot: dropped
        b.on_event(fut(96, 102, 95, b=[("99.0", "4")]))        # bridges id 100
        self.assertTrue(b.needs_snapshot())
        self.assertTrue(b.load_snapshot(SNAP))
        self.assertEqual(b.last_id, 102)
        self.assertEqual(b.bids["99.0"], 4.0)
        self.assertNotIn("97", b.bids)
        self.assertTrue(b.on_event(fut(103, 105, 102, a=[("100.0", "0")])))
        self.assertNotIn("100.0", b.asks)
        self.assertEqual(b.best(), (99.0, 101.0))

    def test_broken_chain_resyncs(self):
        b = LocalBook("futures")
        b.on_event(fut(96, 102, 95))
        b.load_snapshot(SNAP)
        self.assertFalse(b.on_event(fut(110, 112, 108)))      # pu 108 != last 102
        self.assertFalse(b.synced)
        self.assertEqual(b.resyncs, 1)
        self.assertEqual(b.bids, {})
        self.assertTrue(b.needs_snapshot())

    def test_snapshot_newer_than_buffer_then_live_bridge(self):
        b = LocalBook("futures")
        b.on_event(fut(80, 90, 79))
        self.assertTrue(b.load_snapshot(SNAP))                 # nothing buffered reaches id 100
        self.assertTrue(b.on_event(fut(95, 104, 90, b=[("98.5", "7")])))  # first live event bridges
        self.assertEqual(b.bids["98.5"], 7.0)
        self.assertTrue(b.on_event(fut(105, 106, 104)))

    def test_snapshot_older_than_stream(self):
        b = LocalBook("futures")
        b.on_event(fut(150, 160, 149))
        self.assertFalse(b.load_snapshot(SNAP))
        self.assertFalse(b.synced)
        self.assertTrue(b.buffer)


class SpotBook(unittest.TestCase):
    def test_bridge_gap_and_duplicates(self):
        b = LocalBook("spot")
        b.on_event(spot(95, 100))                              # fully covered by the snapshot
        b.on_event(spot(101, 103, a=[("100.0", "2")]))
        self.assertTrue(b.load_snapshot(SNAP))
        self.assertEqual((b.last_id, b.asks["100.0"]), (103, 2.0))
        self.assertTrue(b.on_event(spot(102, 103)))            # stale duplicate is ignored
        self.assertTrue(b.on_event(spot(104, 104, b=[("99.0", "0")])))
        self.assertNotIn("99.0", b.bids)
        self.assertFalse(b.on_event(spot(106, 107)))           # id 105 missing
        self.assertFalse(b.synced)

    def test_snapshot_too_old_keeps_buffer(self):
        b = LocalBook("spot")
        b.on_event(spot(120, 125))
        self.assertFalse(b.load_snapshot(SNAP))
        self.assertEqual(len(b.buffer), 1)

    def test_buckets(self):
        b = LocalBook("spot")
        b.load_snapshot({"lastUpdateId": 1, "bids": [["99.94", "1"], ["99.51", "2"], ["90", "9"]], "asks": [["100.06", "1"], ["100.4", "3"], ["150", "9"]]})
        mid, bids, asks = b.buckets(0.5, 1.0)
        self.assertAlmostEqual(mid, 100.0)
        self.assertEqual(bids, [[99.5, 3.0]])                  # 90 is outside ±1%
        self.assertEqual(asks, [[100.5, 4.0]])


class Sink(unittest.TestCase):
    def test_rotation_recover_and_late_rows(self):
        with tempfile.TemporaryDirectory() as root:
            s = HourlySink(root, clock=FrozenClock(T0 + 3 * H))
            s.write("spot", "BTCUSDT", "trades", T0, [T0, 1, 100.0, 1.0, 0])
            s.write("spot", "BTCUSDT", "trades", T0 + H, [T0 + H, 2, 101.0, 1.0, 1])  # next hour
            first = hour_path(root, "spot", "BTCUSDT", "trades", T0)
            self.assertTrue(os.path.exists(first + ".gz"))
            self.assertFalse(os.path.exists(first))
            s.write("spot", "BTCUSDT", "trades", T0, [T0, 3, 99.0, 1.0, 0])           # late row reopens the closed hour
            s.write("spot", "BTCUSDT", "trades", T0 + H, [T0 + H, 4, 101.0, 1.0, 0])
            s.close()
            self.assertEqual([r[1] for r in read_rows(first + ".gz")], [1, 3])      # two gzip members
            second = hour_path(root, "spot", "BTCUSDT", "trades", T0 + H)
            self.assertEqual(HourlySink(root).recover(now_ms=T0 + 3 * H), 1)
            self.assertEqual([r[1] for r in read_rows(second + ".gz")], [2, 4])

    def test_torn_last_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            p = os.path.join(root, "x.jsonl.gz")
            with gzip.open(p, "wt") as fh:
                fh.write('[1,2]\n[3,')
            self.assertEqual(list(read_rows(p)), [[1, 2]])

    def test_truncated_gzip_tail_ends_the_file_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as root:
            p = os.path.join(root, "x.jsonl.gz")
            with gzip.open(p, "wt") as fh:
                fh.write("".join(f"[{i}]\n" for i in range(2000)))
            with open(p, "rb") as fh:
                data = fh.read()
            with open(p, "wb") as fh:
                fh.write(data[: len(data) // 2])
            rows = list(read_rows(p))
            self.assertLess(len(rows), 2000)
            self.assertEqual(rows, [[i] for i in range(len(rows))])

    def test_torn_line_is_terminated_before_appending(self):
        with tempfile.TemporaryDirectory() as root:
            path = hour_path(root, "spot", "BTCUSDT", "trades", T0)
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as fh:
                fh.write('[1,1]\n[2,')                       # crash mid-row
            s = HourlySink(root)
            s.write("spot", "BTCUSDT", "trades", T0, [T0, 3])
            s.close()
            self.assertEqual(list(read_rows(path)), [[1, 1], [T0, 3]])

    def test_manifest_describes_each_compressed_file(self):
        with tempfile.TemporaryDirectory() as root:
            clock = FrozenClock(T0 + 2 * H)
            s = HourlySink(root, clock=clock)
            for i, t in enumerate((T0 + 5, T0 + 1, T0 + 9)):              # out of order inside the hour
                s.write("futures", "XAUUSDT", "trades", T0, [t, i])
            s.write("futures", "XAUUSDT", "trades", T0 + H, [T0 + H, 9])    # rotation compresses hour T0
            gz = hour_path(root, "futures", "XAUUSDT", "trades", T0) + ".gz"
            rel = os.path.relpath(gz, root).replace(os.sep, "/")
            m = read_manifest(root)[rel]
            with open(gz, "rb") as fh:
                blob = fh.read()
            self.assertEqual((m["rows"], m["first_ts"], m["last_ts"], m["closed_at"]), (3, T0 + 1, T0 + 9, T0 + 2 * H))
            self.assertEqual((m["bytes"], m["sha256"]), (len(blob), hashlib.sha256(blob).hexdigest()))
            # a late row reopens the hour: the newest line for the path covers both members
            clock.advance(H)
            s.write("futures", "XAUUSDT", "trades", T0, [T0 - 5, 10])
            s.write("futures", "XAUUSDT", "trades", T0 + H, [T0 + H + 1, 11])
            s.close()
            with open(os.path.join(root, "manifest.jsonl")) as fh:
                lines = [json.loads(x) for x in fh]
            self.assertEqual([x["path"] for x in lines], [rel, rel])
            m = read_manifest(root)[rel]
            with open(gz, "rb") as fh:
                blob = fh.read()
            self.assertEqual((m["rows"], m["first_ts"], m["last_ts"], m["closed_at"]), (4, T0 - 5, T0 + 9, T0 + 3 * H))
            self.assertEqual(m["sha256"], hashlib.sha256(blob).hexdigest())
            self.assertEqual([r[1] for r in read_rows(gz)], [0, 1, 2, 10])

    def test_recover_compresses_leftovers_but_not_the_current_hour(self):
        with tempfile.TemporaryDirectory() as root:
            s = HourlySink(root)
            s.write("spot", "BTCUSDT", "book", T0, [T0, 1])
            s.write("spot", "BTCUSDT", "kline", T0 + H, [T0 + H, 2])
            s.close()                                                          # "crash": nothing compressed
            old = hour_path(root, "spot", "BTCUSDT", "book", T0)
            cur = hour_path(root, "spot", "BTCUSDT", "kline", T0 + H)
            self.assertEqual(HourlySink(root, clock=FrozenClock(T0 + H + 60_000)).recover(), 1)
            self.assertTrue(os.path.exists(old + ".gz") and not os.path.exists(old))
            self.assertTrue(os.path.exists(cur) and not os.path.exists(cur + ".gz"))
            self.assertEqual(list(read_rows(old + ".gz")), [[T0, 1]])
            self.assertEqual(read_manifest(root)[os.path.relpath(old + ".gz", root).replace(os.sep, "/")]["rows"], 1)

    def test_recover_finishes_or_redoes_an_interrupted_compression(self):
        with tempfile.TemporaryDirectory() as root:
            a = hour_path(root, "spot", "BTCUSDT", "trades", T0)
            b = hour_path(root, "spot", "BTCUSDT", "trades", T0 + H)
            os.makedirs(os.path.dirname(a))
            for p, n in ((a, 1), (b, 2)):
                with open(p, "w") as fh:
                    fh.write(f"[{n}]\n")
                compress(p)
            # a: crashed after the commit point (plain removed, tmp complete, gz not yet renamed)
            os.replace(a + ".gz", a + ".gz.tmp")
            # b: crashed before it (plain still there, tmp half written)
            with open(b, "w") as fh:
                fh.write("[2]\n")
            os.remove(b + ".gz")
            with open(b + ".gz.tmp", "wb") as fh:
                fh.write(b"\x1f\x8b garbage")
            self.assertEqual(HourlySink(root).recover(now_ms=T0 + 5 * H), 1)
            self.assertEqual(list(read_rows(a + ".gz")), [[1]])
            self.assertEqual(list(read_rows(b + ".gz")), [[2]])                # redone once, not duplicated
            self.assertEqual(glob.glob(os.path.join(root, "**", "*.tmp"), recursive=True), [])
            self.assertEqual(len(stream_files(root, "spot", "BTCUSDT", "trades")), 2)

    def test_periodic_fsync(self):
        with tempfile.TemporaryDirectory() as root:
            s = HourlySink(root, sync_every_s=0.0)
            s.write("spot", "BTCUSDT", "trades", T0, [T0, 1])
            with mock.patch("live.sink.os.fsync") as fsync:
                s.flush()
            self.assertEqual(fsync.call_count, 1)
            s2 = HourlySink(root, sync_every_s=3600.0)
            s2.write("spot", "BTCUSDT", "book", T0, [T0, 1])
            with mock.patch("live.sink.os.fsync") as fsync:
                s2.flush()
                self.assertEqual(fsync.call_count, 0)                         # not due yet
                s2.sync()
                self.assertEqual(fsync.call_count, 1)
            s.close()
            s2.close()


CFG = {"markets": {"futures": {"rest": "http://x", "ws": "ws://x", "symbols": ["XAUUSDT"]}}, "book": {"XAUUSDT": {"bucket": 0.5}}}


def agg(a, t=None):
    return {"a": a, "p": "4000", "q": "1", "T": T0 + a if t is None else t, "m": a % 2 == 0}


def trade_msg(a):
    return {"stream": "x", "data": {"e": "aggTrade", "s": "XAUUSDT", "E": T0, "T": T0 + a, "a": a, "p": "4000", "q": "2", "m": False}}


def collector(root, clock, cfg=CFG):
    col = Collector(cfg, root, os.path.join(root, "state.json"), clock=clock)
    col.retry_base_s, col.page_pause_s = 0.01, 0
    return col


async def settle(col, sym, timeout=5.0):
    """Wait until the backfill task has nothing left to do."""
    for _ in range(int(timeout / 0.01)):
        await asyncio.sleep(0.01)
        if not sym.backfilling:
            return
    raise AssertionError("backfill did not finish")


class Handling(unittest.TestCase):
    def test_gap_backfill_kline_liq(self):
        clock = FrozenClock(T0 + 3 * H)  # backfill writes in a different hour than the live rows

        async def scenario(root):
            col = collector(root, clock)
            asked = []

            async def rest(market, name, params):
                asked.append((name, params["fromId"]))
                return [agg(i) for i in range(params["fromId"], params["fromId"] + 3)]

            col.rest = rest
            for a in (10, 11, 15):
                col.handle("futures", trade_msg(a))
            col.handle("futures", {"data": {"e": "aggTrade", "s": "XAUUSDT", "E": T0, "T": T0, "a": 11, "p": "1", "q": "1", "m": True}})  # duplicate
            col.handle("futures", {"data": {"e": "kline", "s": "XAUUSDT", "E": T0, "k": {"t": T0, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "9", "n": 4, "V": "5", "x": False}}})
            col.handle("futures", {"data": {"e": "kline", "s": "XAUUSDT", "E": T0, "k": {"t": T0, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "9", "n": 4, "V": "5", "x": True}}})
            col.handle("futures", {"data": {"e": "forceOrder", "E": T0, "o": {"s": "XAUUSDT", "S": "SELL", "p": "4000", "ap": "3999", "q": "3", "X": "FILLED", "T": T0}}})
            await settle(col, col.syms[("futures", "XAUUSDT")])
            col.sink.close()
            return col, asked

        with tempfile.TemporaryDirectory() as root:
            col, asked = asyncio.run(scenario(root))
            sym = col.syms[("futures", "XAUUSDT")]
            self.assertEqual(asked, [("agg", 12)])
            self.assertEqual((sym.n["trades"], sym.n["backfilled"], sym.n["kline"], sym.n["liq"]), (3, 3, 1, 1))
            self.assertEqual(sorted(r[1] for r in stream_rows(root, "futures", "XAUUSDT", "trades")), [10, 11, 12, 13, 14, 15])
            self.assertEqual(sym.gaps, [])
            c = coverage(root, "futures", "XAUUSDT", "2026-10-18", now_ms=clock())
            self.assertEqual(c["trades"]["id_continuity_pct"], 100.0)
            liq = stream_rows(root, "futures", "XAUUSDT", "liq")
            self.assertEqual(liq[0][1:3], ["SELL", 4000.0])
            meta = stream_rows(root, "futures", "XAUUSDT", "meta")
            self.assertEqual([r[1:] for r in meta], [["backfill", 12, 14, 3]])
            self.assertEqual(meta[0][0], clock())  # meta rows carry the injected clock

    def test_status_and_meta_use_the_injected_clock(self):
        with tempfile.TemporaryDirectory() as root:
            clock = FrozenClock(T0)
            col = collector(root, clock)
            clock.advance(90_000)
            col.write_status()
            with open(os.path.join(root, "status.json")) as fh:
                st = json.load(fh)
            self.assertEqual((st["updated"], st["uptime_s"]), (T0 + 90_000, 90))
            col._save_state()
            with open(os.path.join(root, "state.json")) as fh:
                self.assertEqual(json.load(fh)["saved"], T0 + 90_000)

    def test_depth_diffs_stored_only_when_enabled(self):
        diff = {"data": {"e": "depthUpdate", "s": "XAUUSDT", "E": T0, "T": T0 - 2, "U": 5, "u": 9, "pu": 4,
                         "b": [["4000.1", "2.5"]], "a": [["4000.3", "0"]]}}

        async def scenario(root, cfg):
            col = collector(root, FrozenClock(T0 + 7), cfg)

            async def rest(market, name, params):
                return {"lastUpdateId": 6, "bids": [["4000.0", "1"]], "asks": [["4000.5", "1"]]}

            col.rest = rest
            col.handle("futures", diff)
            sym = col.syms[("futures", "XAUUSDT")]
            for _ in range(300):
                await asyncio.sleep(0.01)
                if sym.book.synced:
                    break
            col.sink.close()
            return sym

        for flag in (False, True):
            with tempfile.TemporaryDirectory() as root:
                sym = asyncio.run(scenario(root, {**CFG, "store_depth_diffs": flag}))
                self.assertTrue(sym.book.synced)
                depth = stream_rows(root, "futures", "XAUUSDT", "depth")
                snaps = stream_rows(root, "futures", "XAUUSDT", "depth_snap")
                if not flag:
                    self.assertEqual((depth, snaps), ([], []))
                    continue
                self.assertEqual(depth, [[T0 + 7, T0, T0 - 2, 5, 9, 4, [["4000.1", "2.5"]], [["4000.3", "0"]]]])
                self.assertEqual(snaps, [[T0 + 7, 6, [["4000.0", "1"]], [["4000.5", "1"]]]])


class Backfill(unittest.TestCase):
    def test_rest_failure_mid_backfill_is_retried_and_completed(self):
        clock = FrozenClock(T0)

        async def scenario(root):
            col = collector(root, clock)
            calls = []

            async def rest(market, name, params):
                calls.append(params["fromId"])
                if len(calls) == 2:
                    raise ConnectionError("reset by peer")
                lo = params["fromId"]
                return [agg(i) for i in range(lo, min(lo + params["limit"], 2_600))]

            col.rest = rest
            col.handle("futures", trade_msg(1))
            col.handle("futures", trade_msg(2_500))                # ids 2..2499 missing: three pages
            sym = col.syms[("futures", "XAUUSDT")]
            self.assertEqual(sym.gaps[0]["status"], "pending")
            await settle(col, sym)
            col.sink.close()
            return col, sym, calls

        with tempfile.TemporaryDirectory() as root:
            col, sym, calls = asyncio.run(scenario(root))
            self.assertEqual(calls, [2, 1002, 1002, 2002])         # the failed page is asked again, nothing skipped
            ids = sorted({r[1] for r in stream_rows(root, "futures", "XAUUSDT", "trades")})
            self.assertEqual(ids, list(range(1, 2_501)))
            self.assertEqual((sym.gaps, sym.n["backfilled"], sym.n["lost"]), ([], 2_498, 0))
            ev = [r[1:] for r in stream_rows(root, "futures", "XAUUSDT", "meta")]
            self.assertEqual(ev, [["backfill_retry", 2, 2_499, 1_002, 1, "reset by peer"], ["backfill", 2, 2_499, 2_498]])

    def test_failed_range_stays_in_ledger_with_backoff(self):
        async def scenario(root):
            col = collector(root, FrozenClock(T0))
            col.retry_base_s, col.retry_cap_s = 0.01, 0.04

            async def rest(market, name, params):
                raise TimeoutError("timed out")

            col.rest = rest
            col.handle("futures", trade_msg(1))
            col.handle("futures", trade_msg(10))
            await asyncio.sleep(0.3)
            sym = col.syms[("futures", "XAUUSDT")]
            gap = dict(sym.gaps[0])
            col.stop.set()
            await settle(col, sym)
            col._save_state()
            col.sink.close()
            return col, gap

        with tempfile.TemporaryDirectory() as root:
            col, gap = asyncio.run(scenario(root))
            self.assertEqual((gap["lo"], gap["hi"], gap["next"], gap["status"], gap["last_error"]), (2, 9, 2, "pending", "timed out"))
            self.assertGreaterEqual(gap["attempts"], 3)
            self.assertEqual([col.retry_delay(n) for n in (1, 2, 3, 4, 9)], [0.01, 0.02, 0.04, 0.04, 0.04])
            self.assertEqual(Collector(CFG, root, os.path.join(root, "state.json")).retry_delay(20), 300.0)
            with open(os.path.join(root, "state.json")) as fh:
                saved = json.load(fh)["gaps"]["futures:XAUUSDT"]
            self.assertEqual([(g["lo"], g["hi"], g["status"]) for g in saved], [(2, 9, "pending")])

    def test_restart_resumes_pending_ledger(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = [{"lo": 100, "hi": 104, "next": 102, "attempts": 2, "status": "partial", "last_error": "timed out", "got": 2},
                      {"lo": 50, "hi": 60, "next": 50, "attempts": 0, "status": "unrecoverable", "last_error": "beyond BACKFILL_MAX", "got": 0}]
            with open(os.path.join(root, "state.json"), "w") as fh:
                json.dump({"last_agg": {"futures:XAUUSDT": 200}, "gaps": {"futures:XAUUSDT": ledger}}, fh)

            async def scenario():
                col = collector(root, FrozenClock(T0))
                asked = []

                async def rest(market, name, params):
                    asked.append(params["fromId"])
                    return [agg(i) for i in range(params["fromId"], 110)]

                col.rest = rest
                sym = col.syms[("futures", "XAUUSDT")]
                self.assertEqual((sym.last_agg, len(sym.gaps)), (200, 2))
                col.resume_backfills()                              # what run() does at start
                await settle(col, sym)
                col._save_state()
                col.sink.close()
                return sym, asked

            sym, asked = asyncio.run(scenario())
            self.assertEqual(asked, [102])                          # continues where the ledger stopped
            self.assertEqual(sorted(r[1] for r in stream_rows(root, "futures", "XAUUSDT", "trades")), [102, 103, 104])
            self.assertEqual([g["status"] for g in sym.gaps], ["unrecoverable"])  # kept for the record, never retried
            with open(os.path.join(root, "state.json")) as fh:
                self.assertEqual([g["lo"] for g in json.load(fh)["gaps"]["futures:XAUUSDT"]], [50])

    def test_huge_gap_beyond_backfill_max_is_recorded_unrecoverable(self):
        clock = FrozenClock(T0 + 60_000)

        async def scenario(root):
            col = collector(root, clock)

            async def rest(market, name, params):
                return [agg(i) for i in range(params["fromId"], params["fromId"] + 4)]

            col.rest = rest
            col.handle("futures", trade_msg(1))
            col.handle("futures", trade_msg(20))                    # ids 2..19 missing, only 5 may be fetched
            sym = col.syms[("futures", "XAUUSDT")]
            await settle(col, sym)
            col.sink.close()
            return sym

        with tempfile.TemporaryDirectory() as root, mock.patch.object(collector_mod, "BACKFILL_MAX", 5):
            sym = asyncio.run(scenario(root))
            self.assertEqual([(g["lo"], g["hi"], g["status"], g["last_error"]) for g in sym.gaps], [(7, 19, "unrecoverable", "beyond BACKFILL_MAX")])
            self.assertEqual(sym.n["lost"], 13)
            ids = sorted({r[1] for r in stream_rows(root, "futures", "XAUUSDT", "trades")})
            self.assertEqual(ids, [1, 2, 3, 4, 5, 6, 20])
            meta = [r[1:] for r in stream_rows(root, "futures", "XAUUSDT", "meta")]
            self.assertIn(["trade_gap", 7, 19, "beyond BACKFILL_MAX"], meta)
            c = coverage(root, "futures", "XAUUSDT", "2026-10-18", now_ms=clock())
            self.assertEqual((c["events"]["lost_ids"], c["events"]["gaps"]), (13, [[7, 19, "beyond BACKFILL_MAX"]]))
            self.assertLess(c["trades"]["id_continuity_pct"], 100.0)

    def test_ids_missing_from_exchange_answer_are_recorded_not_skipped(self):
        async def scenario(root):
            col = collector(root, FrozenClock(T0))

            async def rest(market, name, params):
                return [agg(i) for i in (3, 4, 7, 8, 12)]           # 2, 5, 6 absent; 9 answered past the gap end

            col.rest = rest
            col.handle("futures", trade_msg(1))
            col.handle("futures", trade_msg(10))
            sym = col.syms[("futures", "XAUUSDT")]
            await settle(col, sym)
            col.sink.close()
            return sym

        with tempfile.TemporaryDirectory() as root:
            sym = asyncio.run(scenario(root))
            self.assertEqual([(g["lo"], g["hi"]) for g in sym.gaps], [(2, 2), (5, 6), (9, 9)])
            self.assertEqual(sym.n["lost"], 4)
            meta = [r[1:4] for r in stream_rows(root, "futures", "XAUUSDT", "meta")]
            self.assertEqual(meta, [["trade_gap", 2, 2], ["trade_gap", 5, 6], ["trade_gap", 9, 9], ["backfill", 2, 9]])


# -- 1m klines over REST: history at start, the disconnected span after a reconnect ---------------
MIN = 60_000
HI = T0 // MIN * MIN - MIN          # open time of the newest candle closed at T0; HI + MIN is still open
SPOT = {"markets": {"spot": {"rest": "http://x", "ws": "ws://x", "symbols": ["BTCUSDT"]}}, "book": {"BTCUSDT": {"bucket": 10}}}


def rest_kline(t, close=None):
    """A /api/v3/klines (or /fapi/v1/klines) row: open, o, h, l, c, v, close time, quote v, trades, taker buy base, ..."""
    return [t, "100.0", "101.5", "99.5", "100.5", "12.5", t + 59_999 if close is None else close, "1250", 42, "7.25", "725", "0"]


def ws_kline(symbol, t, closed=True, E=None):
    return {"stream": "x", "data": {"e": "kline", "s": symbol, "E": t + MIN + 5 if E is None else E,
                                    "k": {"t": t, "o": "100", "h": "102", "l": "99", "c": "101", "v": "3", "n": 9, "V": "1", "x": closed}}}


def kline_rest(calls, fail=None):
    """Fake REST: candles from startTime to endTime (at most ``limit``); ``fail(n, params)`` may return an error to raise."""
    async def rest(market, name, params):
        calls.append((market, name, params["startTime"], params["endTime"], params["limit"]))
        err = fail(len(calls), params) if fail else None
        if err is not None:
            raise err
        rows, t = [], params["startTime"]
        while t <= params["endTime"] and len(rows) < params["limit"]:
            rows.append(rest_kline(t))
            t += MIN
        return rows
    return rest


async def settle_klines(sym, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        await asyncio.sleep(0.01)
        if not sym.kline_busy:
            return
    raise AssertionError("kline jobs did not finish")


def history_run(root, cfg, market, symbol, rest, clock=None, before=None, attempts=None):
    """Run the start-up history fetch only; ``before(col)`` runs first (e.g. socket klines already written)."""
    async def scenario():
        col = collector(root, clock or FrozenClock(T0), cfg)
        col.rest = rest
        if attempts:
            col.kline_attempts = attempts
        if before:
            before(col)
        col.start_kline_history()
        sym = col.syms[(market, symbol)]
        await settle_klines(sym)
        col.sink.close()
        return col, sym
    return asyncio.run(scenario())


class KlineHistory(unittest.TestCase):
    def test_default_and_pages_by_market_limit(self):
        self.assertEqual(Collector(SPOT, tempfile.gettempdir(), os.devnull).kline_history_min, 1440)
        for market, n, limits in (("spot", 1440, [1000, 440]), ("futures", 1440, [1440]), ("futures", 3100, [1500, 1500, 100])):
            with self.subTest(market=market, n=n), tempfile.TemporaryDirectory() as root:
                cfg = {"markets": {market: {"rest": "http://x", "ws": "ws://x", "symbols": ["BTCUSDT"]}}, "kline_history_min": n}
                calls = []
                col, sym = history_run(root, cfg, market, "BTCUSDT", kline_rest(calls))
                lo = HI - (n - 1) * MIN
                self.assertEqual([c[4] for c in calls], limits)
                self.assertEqual([c[2] for c in calls], [lo + sum(limits[:i]) * MIN for i in range(len(limits))])
                self.assertTrue(all(c[:2] == (market, "klines") and c[3] == HI + MIN - 1 for c in calls))
                opens = [r[0] for r in stream_rows(root, market, "BTCUSDT", "kline")]
                self.assertEqual(sorted(opens), list(range(lo, HI + 1, MIN)))   # every minute exactly once
                self.assertEqual([r[1:] for r in stream_rows(root, market, "BTCUSDT", "meta")], [["kline_history", lo, HI, n]])
                self.assertEqual((sym.n["kline_rest"], sym.n["kline"], sym.n["kline_missing"]), (n, 0, 0))

    def test_only_closed_candles_are_written(self):
        async def rest(market, name, params):
            # answers past endTime up to the open HI + MIN candle, and says HI closes at the clock time (not before it)
            return [rest_kline(t, close=T0 if t == HI else None) for t in range(params["startTime"], HI + 2 * MIN, MIN)]

        with tempfile.TemporaryDirectory() as root:
            history_run(root, {**SPOT, "kline_history_min": 5}, "spot", "BTCUSDT", rest)
            self.assertEqual([r[0] for r in stream_rows(root, "spot", "BTCUSDT", "kline")], list(range(HI - 4 * MIN, HI, MIN)))
            self.assertEqual([r[1:] for r in stream_rows(root, "spot", "BTCUSDT", "meta")],
                             [["kline_history", HI - 4 * MIN, HI - MIN, 4], ["kline_gap", HI, HI, "not closed by the collector clock"]])

    def test_no_minute_written_twice_with_the_socket_or_an_earlier_run(self):
        with tempfile.TemporaryDirectory() as root:
            earlier = HourlySink(root)                                   # an earlier run left HI - 20 min on disk
            earlier.write("spot", "BTCUSDT", "kline", HI - 20 * MIN + MIN, [HI - 20 * MIN, 1.0, 1.0, 1.0, 1.0, 0.0, 0, 0.0])
            earlier.close()
            calls = []
            col, sym = history_run(root, {**SPOT, "kline_history_min": 30}, "spot", "BTCUSDT", kline_rest(calls),
                                   before=lambda c: c.handle("spot", ws_kline("BTCUSDT", HI - 10 * MIN)))  # socket, this run
            col.handle("spot", ws_kline("BTCUSDT", HI))                  # the socket repeats a minute REST wrote: skipped
            col.handle("spot", ws_kline("BTCUSDT", HI + MIN))            # a new minute: written
            col.sink.close()
            opens = [r[0] for r in stream_rows(root, "spot", "BTCUSDT", "kline")]
            lo = HI - 29 * MIN
            self.assertEqual(max(Counter(opens).values()), 1)
            self.assertEqual(sorted(opens), list(range(lo, HI + 2 * MIN, MIN)))
            self.assertEqual([r[1:] for r in stream_rows(root, "spot", "BTCUSDT", "meta")],
                             [["kline_history", lo, HI - 21 * MIN, 9], ["kline_history", HI - 19 * MIN, HI - 11 * MIN, 9],
                              ["kline_history", HI - 9 * MIN, HI, 10]])
            self.assertEqual((sym.n["kline"], sym.n["kline_rest"]), (2, 28))
            # live.check keeps the REST candles out of the socket coverage
            k = coverage(root, "spot", "BTCUSDT", "2026-10-18", now_ms=T0)["kline"]
            expected = (T0 - day_start("2026-10-18")) // MIN               # minutes closed by T0
            self.assertEqual((k["minutes"], k["rest_minutes"], k["complete_minutes"], k["expected"]), (2, 28, 30, expected))
            self.assertAlmostEqual(k["pct"], 100.0 * 2 / expected)

    def test_rest_failure_is_retried_then_recorded_as_kline_gap(self):
        lo = HI - 9 * MIN
        with tempfile.TemporaryDirectory() as root:                      # one transient failure: retried, nothing lost
            calls = []
            history_run(root, {**SPOT, "kline_history_min": 10}, "spot", "BTCUSDT",
                        kline_rest(calls, fail=lambda n, p: ConnectionError("reset by peer") if n == 1 else None))
            self.assertEqual(len(calls), 2)
            self.assertEqual([r[1:] for r in stream_rows(root, "spot", "BTCUSDT", "meta")],
                             [["kline_retry", lo, HI, lo, 1, "reset by peer"], ["kline_history", lo, HI, 10]])

        with tempfile.TemporaryDirectory() as root:                      # never answers: kline_gap after the attempts
            calls = []

            async def scenario():
                col = collector(root, FrozenClock(T0), {**SPOT, "kline_history_min": 10})
                col.rest, col.kline_attempts = kline_rest(calls, fail=lambda n, p: TimeoutError("timed out")), 3
                col.start_kline_history()
                sym = col.syms[("spot", "BTCUSDT")]
                await settle_klines(sym)
                col.on_connect("spot")                                    # the socket then connects
                col.handle("spot", ws_kline("BTCUSDT", HI + MIN, closed=False))
                await settle_klines(sym)
                col.sink.close()
                return sym

            sym = asyncio.run(scenario())
            self.assertEqual(len(calls), 3)                              # the recorded gap is not asked for again
            meta = [r[1:] for r in stream_rows(root, "spot", "BTCUSDT", "meta")]
            self.assertEqual(meta[:3], [["kline_retry", lo, HI, lo, i, "timed out"] for i in (1, 2, 3)])
            self.assertEqual(meta[3:], [["kline_gap", lo, HI, "REST failed 3 times: timed out"], ["connected"]])
            self.assertEqual((stream_rows(root, "spot", "BTCUSDT", "kline"), sym.n["kline_missing"]), ([], 10))

        with tempfile.TemporaryDirectory() as root:                      # second page fails: the first page stays
            lo = HI - 1439 * MIN
            history_run(root, SPOT, "spot", "BTCUSDT", kline_rest([], fail=lambda n, p: OSError("reset") if n > 1 else None),
                        attempts=2)
            meta = [r[1:] for r in stream_rows(root, "spot", "BTCUSDT", "meta")]
            self.assertEqual(meta, [["kline_retry", lo, HI, lo + 1000 * MIN, 1, "reset"], ["kline_retry", lo, HI, lo + 1000 * MIN, 2, "reset"],
                                    ["kline_history", lo, lo + 999 * MIN, 1000], ["kline_gap", lo + 1000 * MIN, HI, "REST failed 2 times: reset"]])
            self.assertEqual(len(stream_rows(root, "spot", "BTCUSDT", "kline")), 1000)

    def test_blocked_451_and_403_are_recorded_not_retried(self):
        lo = HI - 9 * MIN
        for code, err, text in ((451, collector_mod.Blocked("http://x/api/v3/klines"), collector_mod.BLOCKED),
                                (403, urllib.error.HTTPError("http://x/api/v3/klines", 403, "Forbidden", {}, None), collector_mod.BLOCKED_403)):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as root:
                calls = []
                col, sym = history_run(root, {**SPOT, "kline_history_min": 10}, "spot", "BTCUSDT", kline_rest(calls, fail=lambda n, p: err))
                self.assertEqual(len(calls), 1)
                self.assertEqual(col.blocked, {"spot": text})
                self.assertEqual([r[1:] for r in stream_rows(root, "spot", "BTCUSDT", "meta")], [["kline_gap", lo, HI, f"blocked ({code})"]])


class KlineReconnect(unittest.TestCase):
    def test_disconnected_span_is_backfilled(self):
        m0 = HI - 5 * MIN                                                # m0 + 6 min is the candle open at T0

        async def scenario(root):
            col = collector(root, FrozenClock(T0), {**CFG, "kline_history_min": 0})
            calls = []
            col.rest = kline_rest(calls)
            sym = col.syms[("futures", "XAUUSDT")]
            col.on_connect("futures")
            col.handle("futures", ws_kline("XAUUSDT", m0, closed=False))  # history off: nothing to measure from yet
            col.handle("futures", ws_kline("XAUUSDT", m0))
            col.handle("futures", ws_kline("XAUUSDT", m0 + MIN))
            col.meta(sym, "disconnected", "reset")                       # what market_loop writes, then it reconnects
            col.on_connect("futures")
            col.handle("futures", ws_kline("XAUUSDT", m0 + 6 * MIN, closed=False))
            await settle_klines(sym)
            col.handle("futures", ws_kline("XAUUSDT", m0 + 6 * MIN))
            col.sink.close()
            return sym, calls

        with tempfile.TemporaryDirectory() as root:
            sym, calls = asyncio.run(scenario(root))
            self.assertEqual(calls, [("futures", "klines", m0 + 2 * MIN, m0 + 6 * MIN - 1, 4)])
            opens = [r[0] for r in stream_rows(root, "futures", "XAUUSDT", "kline")]
            self.assertEqual(sorted(opens), list(range(m0, m0 + 7 * MIN, MIN)))
            self.assertEqual([r[1:] for r in stream_rows(root, "futures", "XAUUSDT", "meta")],
                             [["connected"], ["disconnected", "reset"], ["connected"], ["kline_backfill", m0 + 2 * MIN, m0 + 5 * MIN, 4]])
            self.assertEqual((sym.n["kline"], sym.n["kline_rest"]), (3, 4))

    def test_reconnect_through_the_websocket_loop(self):
        """run() with a fake socket: history at start, a disconnect, the missed minutes over REST, --minutes-style stop."""
        clock = FrozenClock(T0)
        calls = []
        box = {}

        class Socket:
            def __init__(self, script):
                self.script = list(script)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def recv(self):
                item = self.script.pop(0)
                if isinstance(item, Exception):
                    raise item
                if callable(item):
                    return await item()
                clock.t = max(clock.t, item["data"]["E"])                # the socket's event time drives the clock
                return json.dumps(item)

        async def until_backfilled():
            sym = box["col"].syms[("spot", "BTCUSDT")]
            for _ in range(500):
                if sym.n["kline_rest"] >= 33 and not sym.kline_busy:
                    break
                await asyncio.sleep(0.01)
            box["col"].stop.set()
            raise ConnectionError("closed")

        sessions = [Socket([ws_kline("BTCUSDT", HI + MIN, closed=False, E=T0 + 1000), ws_kline("BTCUSDT", HI + MIN, E=HI + 2 * MIN + 100),
                            ConnectionError("reset by peer")]),
                    Socket([ws_kline("BTCUSDT", HI + 5 * MIN, closed=False, E=HI + 5 * MIN + 5000), until_backfilled])]
        client = types.ModuleType("websockets.asyncio.client")
        client.connect = lambda url, **kw: sessions.pop(0)
        fake = {"websockets": types.ModuleType("websockets"), "websockets.asyncio": types.ModuleType("websockets.asyncio"),
                "websockets.asyncio.client": client}

        with tempfile.TemporaryDirectory() as root, mock.patch.dict(sys.modules, fake):
            col = box["col"] = collector(root, clock, {**SPOT, "kline_history_min": 30})
            col.rest, col.reconnect_base_s = kline_rest(calls), 0.01
            asyncio.run(col.run())
            lo = HI - 29 * MIN
            self.assertEqual([c[2:] for c in calls], [(lo, HI + MIN - 1, 30), (HI + 2 * MIN, HI + 5 * MIN - 1, 3)])
            opens = [r[0] for r in stream_rows(root, "spot", "BTCUSDT", "kline")]
            self.assertEqual(sorted(opens), list(range(lo, HI + 5 * MIN, MIN)))  # 30 history, HI+1 socket, HI+2..HI+4 REST
            meta = [r[1:] for r in stream_rows(root, "spot", "BTCUSDT", "meta")]
            self.assertEqual([m for m in meta if m[0].startswith("kline")],
                             [["kline_history", lo, HI, 30], ["kline_backfill", HI + 2 * MIN, HI + 4 * MIN, 3]])
            self.assertEqual(Counter(m[0] for m in meta)["connected"], 2)
            self.assertEqual(col.reconnects["spot"], 1)
            with open(os.path.join(root, "status.json")) as fh:
                st = json.load(fh)["symbols"]["spot:BTCUSDT"]
            self.assertEqual((st["kline"], st["kline_rest"], st["kline_missing"], st["kline_jobs"]), (1, 33, 0, 0))


class KlineRowsIngestable(unittest.TestCase):
    def rows(self, root):
        history_run(root, {**SPOT, "kline_history_min": 3}, "spot", "BTCUSDT", kline_rest([]),
                    before=lambda c: c.handle("spot", ws_kline("BTCUSDT", HI - 3 * MIN)))
        return stream_rows(root, "spot", "BTCUSDT", "kline")

    def test_rest_rows_have_the_socket_row_format(self):
        with tempfile.TemporaryDirectory() as root:
            rows = sorted(self.rows(root))
            self.assertEqual(rows[0], [HI - 3 * MIN, 100.0, 102.0, 99.0, 101.0, 3.0, 9, 1.0])           # socket
            self.assertEqual(rows[-1], [HI, 100.0, 101.5, 99.5, 100.5, 12.5, 42, 7.25])                 # REST
            for r in rows:
                self.assertEqual([type(x) for x in r], [int, float, float, float, float, float, int, float])

    @unittest.skipUnless(importlib.util.find_spec("psycopg"), "server/ingest.py needs psycopg")
    def test_server_ingest_loads_them(self):
        spec = importlib.util.spec_from_file_location("ingest_under_test", Path(__file__).resolve().parents[1] / "server" / "ingest.py")
        ingest = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ingest)
        with tempfile.TemporaryDirectory() as root:
            self.rows(root)
            ing = ingest.Ingestor(None, Path(root))
            got = []
            ing._copy_insert = lambda tmp_cols, target, cols, data, conflict: got.extend(data) or len(data)
            lines = [(ln, ingest.parse(ln)) for p, _, _ in ingest.iter_files(Path(root)) if "/kline/" in p.as_posix()
                     for ln in ingest.read_lines(p)]
            n, bad = ing.load_klines(1, lines)
            self.assertEqual((n, bad), (4, 0))
            self.assertEqual(sorted(g[2] for g in got), [ingest.ms(HI - k * MIN) for k in (3, 2, 1, 0)])


if __name__ == "__main__":
    unittest.main()
