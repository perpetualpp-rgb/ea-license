"""Phase 0 collector: order book sync, hourly storage and message handling."""
import asyncio
import gzip
import os
import tempfile
import unittest

from live.book import LocalBook
from live.check import coverage
from live.collector import Collector
from live.sink import HourlySink, hour_path, read_rows

H = 3600 * 1000
T0 = 1792300000000  # 2026-10-18 04:26:40 UTC


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
            s = HourlySink(root)
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


CFG = {"markets": {"futures": {"rest": "http://x", "ws": "ws://x", "symbols": ["XAUUSDT"]}}, "book": {"XAUUSDT": {"bucket": 0.5}}}


class Handling(unittest.TestCase):
    def test_gap_backfill_kline_liq(self):
        async def scenario(root):
            col = Collector(CFG, root, os.path.join(root, "state.json"))
            asked = []

            async def rest(market, name, params):
                asked.append((name, params["fromId"]))
                return [{"a": i, "p": "4000", "q": "1", "T": T0 + i, "m": i % 2 == 0} for i in range(params["fromId"], params["fromId"] + 3)]

            col.rest = rest
            for a in (10, 11, 15):
                col.handle("futures", {"stream": "x", "data": {"e": "aggTrade", "s": "XAUUSDT", "E": T0, "T": T0 + a, "a": a, "p": "4000", "q": "2", "m": False}})
            col.handle("futures", {"data": {"e": "aggTrade", "s": "XAUUSDT", "E": T0, "T": T0, "a": 11, "p": "1", "q": "1", "m": True}})  # duplicate
            col.handle("futures", {"data": {"e": "kline", "s": "XAUUSDT", "E": T0, "k": {"t": T0, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "9", "n": 4, "V": "5", "x": False}}})
            col.handle("futures", {"data": {"e": "kline", "s": "XAUUSDT", "E": T0, "k": {"t": T0, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "9", "n": 4, "V": "5", "x": True}}})
            col.handle("futures", {"data": {"e": "forceOrder", "E": T0, "o": {"s": "XAUUSDT", "S": "SELL", "p": "4000", "ap": "3999", "q": "3", "X": "FILLED", "T": T0}}})
            await asyncio.sleep(0.6)
            col.sink.close()
            return col, asked

        with tempfile.TemporaryDirectory() as root:
            col, asked = asyncio.run(scenario(root))
            sym = col.syms[("futures", "XAUUSDT")]
            self.assertEqual(asked, [("agg", 12)])
            self.assertEqual((sym.n["trades"], sym.n["backfilled"], sym.n["kline"], sym.n["liq"]), (3, 3, 1, 1))
            day = "2026-10-18"
            ids = sorted(r[1] for r in read_rows(hour_path(root, "futures", "XAUUSDT", "trades", T0)))
            self.assertEqual(ids, [10, 11, 12, 13, 14, 15])
            c = coverage(root, "futures", "XAUUSDT", day)
            self.assertEqual(c["trades"]["pct"], 100.0)
            liq = list(read_rows(hour_path(root, "futures", "XAUUSDT", "liq", T0)))
            self.assertEqual(liq[0][1:3], ["SELL", 4000.0])


if __name__ == "__main__":
    unittest.main()
