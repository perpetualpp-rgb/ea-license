"""Phase 0 collector: order book sync, hourly storage and message handling.

Every Collector here runs on a frozen clock, and rows are read back across all
hour/day partitions of a stream, so no test depends on the wall-clock time or
on which hour file a row landed in."""
import asyncio
import glob
import gzip
import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

import live.collector as collector_mod
from live.book import LocalBook
from live.check import coverage
from live.collector import Collector
from live.sink import HourlySink, compress, hour_path, read_manifest, read_rows, stream_files

H = 3600 * 1000
T0 = 1792300000000  # 2026-10-18 04:26:40 UTC


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


if __name__ == "__main__":
    unittest.main()
