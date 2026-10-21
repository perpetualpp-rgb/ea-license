import unittest

from engine import levels, profile, vwap
from feeds.types import Bar, Trade


def T(ts, price, size, side):
    return Trade(ts, price, size, side, "x", "S", str(ts))


class ProfileTests(unittest.TestCase):
    def test_level_of_snaps_cleanly(self):
        self.assertEqual(profile.level_of(4186.74, 0.1), 4186.7)
        self.assertEqual(profile.level_of(4186.75, 0.1), 4186.8)
        self.assertEqual(profile.level_of(81262.4, 1.0), 81262.0)
        self.assertEqual(profile.level_of(100.3, 0.5), 100.5)

    def test_profile_from_trades_and_poc(self):
        prof = profile.profile_from_trades([T(1, 10.0, 1, "buy"), T(2, 10.04, 2, "sell"),
                                            T(3, 10.1, 1, "buy")], tick=0.1)
        self.assertFalse(prof.approximate)
        self.assertEqual(prof.levels[10.0], {"buy": 1.0, "sell": 2.0, "total": 3.0})
        self.assertEqual(prof.levels[10.1], {"buy": 1.0, "sell": 0.0, "total": 1.0})
        self.assertEqual(profile.poc(prof), 10.0)
        self.assertEqual(prof.total, 4.0)

    def test_value_area_expands_toward_bigger_side(self):
        prof = profile.Profile(1.0)
        for p, v in [(98, 1), (99, 2), (100, 10), (101, 5), (102, 1)]:
            prof.add(p, v, None)
        # 70% of 19 = 13.3 -> POC(10) + 101(5) = 15 >= 13.3
        self.assertEqual(profile.value_area(prof, 0.70), (100.0, 101.0))
        self.assertEqual(profile.value_area(prof, 0.30), (100.0, 100.0))
        r = profile.session_range(prof, 0.70)
        self.assertEqual((r["high"], r["low"], r["poc"], r["va_low"], r["va_high"]), (102.0, 98.0, 100.0, 100.0, 101.0))

    def test_profile_from_bars_conserves_volume(self):
        bars = [Bar(0, 10.0, 10.2, 10.0, 10.1, 30), Bar(60, 10.5, 10.5, 10.5, 10.5, 7)]
        prof = profile.profile_from_bars(bars, 0.1)
        self.assertTrue(prof.approximate)
        self.assertAlmostEqual(prof.total, 37.0)
        self.assertAlmostEqual(prof.levels[10.0]["total"], 10.0)
        self.assertAlmostEqual(prof.levels[10.5]["total"], 7.0)

    def test_nodes_lvn_includes_gaps(self):
        prof = profile.Profile(1.0)
        for p, v in [(100, 5), (101, 1), (104, 6)]:
            prof.add(p, v, None)
        n = profile.nodes(prof, top=2)
        self.assertEqual([x["price"] for x in n["hvn"]], [104.0, 100.0])
        self.assertEqual({x["price"] for x in n["lvn"]}, {102.0, 103.0})

    def test_empty_profile(self):
        prof = profile.Profile(1.0)
        self.assertIsNone(profile.poc(prof))
        self.assertIsNone(profile.value_area(prof))
        self.assertIsNone(profile.session_range(prof))


class VwapTests(unittest.TestCase):
    def test_constant_price_has_zero_sd(self):
        s = vwap.vwap_bands([Bar(0, 5, 5, 5, 5, 3), Bar(60, 5, 5, 5, 5, 7)])
        self.assertEqual(s[-1]["vwap"], 5.0)
        self.assertEqual(s[-1]["sd"], 0.0)
        self.assertEqual(vwap.position(5.0, s[-1])["label"], "at_vwap")

    def test_weighted_vwap(self):
        # typical prices 10 (vol 1) and 20 (vol 3) -> vwap 17.5, var = 0.25*(7.5^2)+0.75*(2.5^2)=18.75
        s = vwap.vwap_bands([Bar(0, 10, 10, 10, 10, 1), Bar(60, 20, 20, 20, 20, 3)], sigmas=(1.0,))
        self.assertAlmostEqual(s[-1]["vwap"], 17.5)
        self.assertAlmostEqual(s[-1]["sd"] ** 2, 18.75)
        lo, hi = s[-1]["bands"][1.0]
        self.assertAlmostEqual(hi - lo, 2 * s[-1]["sd"])
        self.assertEqual(vwap.position(30.0, s[-1])["label"], "expensive")
        self.assertEqual(vwap.position(18.0, s[-1])["label"], "above")
        self.assertEqual(vwap.position(5.0, s[-1])["label"], "cheap")

    def test_band_shape(self):
        series = [{"sd": 1.0}] * 12 + [{"sd": 2.0}]
        self.assertEqual(vwap.band_shape(series, lookback=12), "expanding")
        self.assertEqual(vwap.band_shape([{"sd": 1.0}] * 13, lookback=12), "flat")
        self.assertEqual(vwap.band_shape([{"sd": 1.0}] * 12 + [{"sd": 1.1}], lookback=12), "widening")
        self.assertIsNone(vwap.band_shape([{"sd": 1.0}] * 5, lookback=12))

    def test_zero_volume_bars_skipped(self):
        self.assertEqual(vwap.vwap_bands([Bar(0, 1, 1, 1, 1, 0)]), [])


class LevelTests(unittest.TestCase):
    def test_round_levels(self):
        self.assertEqual(levels.round_levels(4167.5, 4220.4, 25), [4175.0, 4200.0])
        self.assertEqual(levels.round_levels(81000, 82000, 500), [81000.0, 81500.0, 82000.0])

    def test_nearest(self):
        self.assertEqual(levels.nearest(4186.7, 25), (4175.0, 4200.0))
        self.assertEqual(levels.nearest(4200.0, 25), (4200.0, 4200.0))


if __name__ == "__main__":
    unittest.main()
