import unittest

from engine import cvd
from engine.bars import bars_from_trades, bucket_start
from feeds.types import Bar, Trade


def T(ts, price, size, side):
    return Trade(ts, price, size, side, "x", "S", str(ts))


class Bars(unittest.TestCase):
    def test_bucket_start(self):
        self.assertEqual(bucket_start(60_001, 60_000), 60_000)
        self.assertEqual(bucket_start(119_999, 60_000), 60_000)
        self.assertEqual(bucket_start(120_000, 60_000), 120_000)

    def test_bars_from_trades_two_buckets(self):
        trades = [T(1_000, 10, 1, "buy"), T(2_000, 12, 1, "sell"), T(3_000, 9, 2, "buy"),
                  T(61_000, 11, 5, "buy")]
        bars = bars_from_trades(trades, 60_000)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0], Bar(0, 10, 12, 9, 9, 4))
        self.assertEqual(bars[1], Bar(60, 11, 11, 11, 11, 5))

    def test_empty(self):
        self.assertEqual(bars_from_trades([], 60_000), [])


class CVD(unittest.TestCase):
    def test_cvd_series_signs(self):
        s = cvd.cvd_series([T(1, 1, 2, "buy"), T(2, 1, 3, "sell"), T(3, 1, 0.5, "buy")])
        self.assertEqual(s, [(1, 2.0), (2, -1.0), (3, -0.5)])

    def test_delta_buckets_and_cvd(self):
        trades = [T(1_000, 1, 2, "buy"), T(2_000, 1, 3, "sell"), T(61_000, 1, 4, "buy")]
        b = cvd.delta_buckets(trades, 60_000)
        self.assertEqual([(x["t"], x["buy"], x["sell"], x["delta"], x["n"]) for x in b],
                         [(0, 2.0, 3.0, -1.0, 2), (60, 4.0, 0.0, 4.0, 1)])
        self.assertEqual(cvd.cvd_from_buckets(b), [(0, -1.0), (60, 3.0)])

    def test_approx_bar_delta(self):
        self.assertEqual(cvd.approx_bar_delta(Bar(0, 1, 2, 0, 2, 10)), 10.0)   # close at high
        self.assertEqual(cvd.approx_bar_delta(Bar(0, 1, 2, 0, 0, 10)), -10.0)  # close at low
        self.assertEqual(cvd.approx_bar_delta(Bar(0, 1, 2, 0, 1, 10)), 0.0)    # mid
        self.assertEqual(cvd.approx_bar_delta(Bar(0, 1, 1, 1, 1, 10)), 0.0)    # flat bar
        series = cvd.approx_cvd_from_bars([Bar(0, 1, 2, 0, 2, 10), Bar(60, 1, 2, 0, 0, 4)])
        self.assertEqual(series, [(0, 10.0), (60, 6.0)])

    def test_divergence(self):
        prices = [1, 2, 3, 4, 5]
        self.assertEqual(cvd.divergence(prices, [1, 2, 3, 4, 3], lookback=4), "bearish")
        self.assertEqual(cvd.divergence([5, 4, 3, 2, 1], [5, 4, 3, 2, 3], lookback=4), "bullish")
        self.assertIsNone(cvd.divergence(prices, [1, 2, 3, 4, 5], lookback=4))
        self.assertIsNone(cvd.divergence(prices, prices, lookback=10))


if __name__ == "__main__":
    unittest.main()
