import unittest

from engine import levels
from engine.summary import one_liner, summarize_bars, summarize_trades
from feeds.types import BookSnapshot, Trade
from tests.synth import synthetic_bars


def T(ts, price, size, side):
    return Trade(ts, price, size, side, "x", "S", str(ts))


class BarsSummary(unittest.TestCase):
    def test_synthetic_bars_summary(self):
        bars = synthetic_bars()
        r = summarize_bars(bars, tick=0.1, round_step=25)
        self.assertTrue(r["approximate"])
        self.assertEqual(r["bars"], 1500)
        hi, lo, vol = max(b.h for b in bars), min(b.l for b in bars), sum(b.v for b in bars)
        self.assertEqual((r["range"]["high"], r["range"]["low"]), (hi, lo))
        self.assertAlmostEqual(r["volume"], vol)
        self.assertAlmostEqual(r["range"]["total"], vol, places=3)  # bar volume conserved
        self.assertTrue(r["range"]["low"] <= r["magnet"]["low"] <= r["range"]["poc"] <= r["magnet"]["high"] <= r["range"]["high"])
        self.assertTrue(r["range"]["va_low"] <= r["magnet"]["low"] and r["magnet"]["high"] <= r["range"]["va_high"])
        self.assertEqual(r["round_levels"], levels.round_levels(lo, hi, 25))
        self.assertTrue(r["cvd"]["approximate"])
        self.assertIn(r["vwap"]["position"]["label"], ("above", "below", "cheap", "expensive", "at_vwap"))

    def test_empty(self):
        self.assertIn("error", summarize_bars([], 0.1, 25))


class TradesSummary(unittest.TestCase):
    def _book(self, ts, bid=99.9, ask=100.1, wall=None):
        bids = [(round(bid - i * 0.1, 1), 1.0) for i in range(30)]
        asks = [(round(ask + i * 0.1, 1), 1.0) for i in range(30)]
        if wall:
            side, price, size = wall
            lst = bids if side == "bid" else asks
            for i, (p, _) in enumerate(lst):
                if p == price:
                    lst[i] = (p, size)
        return BookSnapshot(ts, "x", "S", tuple(bids), tuple(asks))

    def test_full_summary_shape(self):
        # sizes 0.01..1.20 spread evenly (so P99 is meaningful) plus one 40.0 outlier at i=50
        trades = [T(1_000 * i, 100.0 + (i % 5) * 0.1, 0.01 * i if i != 50 else 40.0, "buy" if i % 3 else "sell")
                  for i in range(1, 121)]
        books = [self._book(0, wall=("bid", 99.0, 20.0)), self._book(70_000, wall=("bid", 99.0, 22.0)), self._book(140_000)]
        r = summarize_trades(trades, books, tick=0.1, round_step=1.0, interval_ms=60_000, persist_ms=60_000, wall_depth=30)
        self.assertEqual(r["mode"], "trades")
        self.assertFalse(r["approximate"])
        self.assertEqual(r["trades"], 120)
        self.assertAlmostEqual(r["profile_buy"] + r["profile_sell"], sum(t.size for t in trades))
        self.assertAlmostEqual(r["cvd"]["value"], sum(t.signed_size for t in trades))
        # P99 of 120 sizes (nearest rank) = 2nd largest = 1.20 -> the 1.20 print and the 40.0 outlier
        self.assertEqual(r["big_trades"]["count"], 2)
        self.assertEqual(r["big_trades"]["zones"][0]["price"], 100.0)
        self.assertEqual(r["big_trades"]["zones"][0]["buy"], 40.0)
        self.assertEqual(r["walls"]["snapshots"], 3)
        # wall seen twice then missing once: still tracked (miss_limit 2), persistent after 70s
        self.assertEqual(r["walls"]["bid"][0]["price"], 99.0)
        self.assertTrue(r["walls"]["bid"][0]["persistent"])
        self.assertEqual(r["walls"]["best_bid"], 99.9)

    def test_no_trades(self):
        self.assertIn("error", summarize_trades([], [], 0.1, 1.0))

    def test_one_liner(self):
        line = one_liner("BTC", [T(1, 100.0, 1.0, "buy"), T(2, 100.0, 0.25, "sell")], self._book(0, wall=("ask", 101.0, 9.0)))
        self.assertIn("delta=+0.7500", line)
        self.assertIn("askwall=101.0x9.000", line)
        self.assertIn("bidwall=-", line)


if __name__ == "__main__":
    unittest.main()
