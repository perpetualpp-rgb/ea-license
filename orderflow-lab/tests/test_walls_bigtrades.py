import unittest

from engine import bigtrades, walls
from feeds.types import BookSnapshot, Trade


def book(ts, bids, asks):
    return BookSnapshot(ts, "x", "S", tuple(bids), tuple(asks))


def flat_book(ts, best_bid=100.0, best_ask=100.1, n=20, size=1.0, tick=0.1, extra=None):
    """Uniform book with optional overrides {(side, price): size}."""
    extra = extra or {}
    bids = [(round(best_bid - i * tick, 1), extra.get(("bid", round(best_bid - i * tick, 1)), size)) for i in range(n)]
    asks = [(round(best_ask + i * tick, 1), extra.get(("ask", round(best_ask + i * tick, 1)), size)) for i in range(n)]
    return book(ts, bids, asks)


class DetectWalls(unittest.TestCase):
    def test_outlier_level_is_a_wall(self):
        b = flat_book(0, extra={("bid", 99.5): 10.0, ("ask", 100.6): 8.0})
        ws = walls.detect_walls(b, min_mult=4.0)
        self.assertEqual([(w["side"], w["price"], w["size"]) for w in ws], [("bid", 99.5, 10.0), ("ask", 100.6, 8.0)])
        self.assertAlmostEqual(ws[0]["dist"], 100.05 - 99.5)
        self.assertAlmostEqual(ws[0]["mult"], 10.0)

    def test_uniform_book_has_no_walls(self):
        self.assertEqual(walls.detect_walls(flat_book(0)), [])

    def test_merge_adjacent(self):
        b = flat_book(0, extra={("ask", 100.6): 8.0, ("ask", 100.7): 9.0})
        ws = walls.detect_walls(b, merge_gap=0.1)
        self.assertEqual(len(ws), 1)
        self.assertEqual((ws[0]["price"], ws[0]["size"], ws[0]["levels"]), (100.7, 17.0, 2))
        self.assertEqual(len(walls.detect_walls(b)), 2)

    def test_min_size_floor(self):
        b = flat_book(0, extra={("bid", 99.5): 10.0})
        self.assertEqual(walls.detect_walls(b, min_size=50.0), [])

    def test_max_dist_pct_ignores_far_levels(self):
        b = flat_book(0, extra={("bid", 99.5): 10.0})  # 0.55% below mid
        self.assertEqual(len(walls.detect_walls(b, max_dist_pct=1.0)), 1)
        self.assertEqual(walls.detect_walls(b, max_dist_pct=0.1), [])
        self.assertEqual(len(walls.detect_walls(b, max_dist_pct=0)), 1)  # 0 disables the filter


class Tracker(unittest.TestCase):
    def test_lifecycle_new_holding_persistent(self):
        tr = walls.WallTracker(tick=0.1, persist_ms=20_000, miss_limit=1)
        tr.update(flat_book(0, extra={("bid", 99.5): 10.0}))
        a = tr.active()
        self.assertEqual((a[0]["status"], a[0]["persistent"], a[0]["seen"]), ("new", False, 1))
        tr.update(flat_book(10_000, extra={("bid", 99.5): 10.5}))
        self.assertEqual(tr.active()[0]["status"], "holding")
        tr.update(flat_book(25_000, extra={("bid", 99.5): 14.0}))  # +33% -> adding
        a = tr.active()
        self.assertEqual((a[0]["status"], a[0]["persistent"], a[0]["seen"]), ("adding", True, 3))
        tr.update(flat_book(30_000, extra={("bid", 99.5): 7.0}))  # -50% -> pulling
        self.assertEqual(tr.active()[0]["status"], "pulling")

    def test_pulled_before_price_arrives(self):
        tr = walls.WallTracker(tick=0.1, miss_limit=1)
        tr.update(flat_book(0, extra={("bid", 99.5): 10.0}))
        tr.update(flat_book(500, extra={("bid", 99.5): 10.0}))  # seen twice -> a real wall, not flicker
        self.assertEqual(tr.update(flat_book(1_000)), [])      # first miss: still tracked
        ev = tr.update(flat_book(2_000))                        # second miss: resolved
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["side"], ev[0]["price"], ev[0]["outcome"]), ("bid", 99.5, "pulled"))
        self.assertEqual(tr.active(), [])
        self.assertEqual(tr.events()[-1]["outcome"], "pulled")

    def test_flicker_is_dropped_without_event(self):
        tr = walls.WallTracker(tick=0.1, miss_limit=0, min_seen=2)
        tr.update(flat_book(0, extra={("bid", 99.5): 10.0}))   # seen once
        self.assertEqual(tr.update(flat_book(1_000)), [])      # gone: no event, no longer active
        self.assertEqual(tr.active(), [])
        self.assertEqual(tr.events(), [])

    def test_filled_when_price_trades_through(self):
        tr = walls.WallTracker(tick=0.1, miss_limit=0, min_seen=1)
        tr.update(flat_book(0, extra={("bid", 99.5): 10.0}))
        # price fell through the wall: best bid now below 99.5 and wall gone
        ev = tr.update(flat_book(1_000, best_bid=99.3, best_ask=99.4))
        self.assertEqual(ev[0]["outcome"], "filled")

    def test_ask_wall_filled(self):
        tr = walls.WallTracker(tick=0.1, miss_limit=0, min_seen=1)
        tr.update(flat_book(0, extra={("ask", 100.6): 10.0}))
        ev = tr.update(flat_book(1_000, best_bid=100.7, best_ask=100.8))
        self.assertEqual(ev[0]["outcome"], "filled")


def T(ts, price, size, side):
    return Trade(ts, price, size, side, "x", "S", str(ts))


class BigTrades(unittest.TestCase):
    def test_percentile_nearest_rank(self):
        vals = list(range(1, 101))
        self.assertEqual(bigtrades.percentile(vals, 0.99), 99)
        self.assertEqual(bigtrades.percentile(vals, 0.95), 95)
        self.assertEqual(bigtrades.percentile(vals, 1.0), 100)
        self.assertEqual(bigtrades.percentile([], 0.5), 0.0)

    def test_threshold_and_filter(self):
        trades = [T(i, 100, s, "buy") for i, s in enumerate([1, 1, 1, 1, 1, 1, 1, 1, 1, 50])]
        thr = bigtrades.threshold(trades, pct=0.95)
        self.assertEqual(thr, 50)
        self.assertEqual(len(bigtrades.big_trades(trades, thr)), 1)
        self.assertEqual(bigtrades.threshold(trades, pct=0.95, min_abs=80), 80)
        self.assertEqual(bigtrades.big_trades(trades, 0.0), [])

    def test_zones_and_contested(self):
        big = [T(1, 100.02, 10, "buy"), T(2, 99.98, 6, "sell"), T(3, 105.0, 20, "sell")]
        z = bigtrades.zones(big, tick=0.1, contest_ratio=0.5)
        self.assertEqual([r["price"] for r in z], [105.0, 100.0])
        self.assertEqual((z[1]["buy"], z[1]["sell"], z[1]["n"], z[1]["contested"], z[1]["delta"]), (10, 6, 2, True, 4))
        self.assertFalse(z[0]["contested"])
        self.assertEqual((z[1]["first_ts"], z[1]["last_ts"]), (1, 2))

    def test_recent(self):
        big = [T(1_000, 1, 1, "buy"), T(50_000, 1, 1, "buy")]
        self.assertEqual([t.ts for t in bigtrades.recent(big, 60_000, 30_000)], [50_000])


if __name__ == "__main__":
    unittest.main()
