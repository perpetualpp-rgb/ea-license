"""Desk journal rules (desk/journal.js) run under node: the same file the desk page inlines.
Skipped when node is not installed."""
import json
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(__file__)
HARNESS = os.path.join(HERE, "js_journal.js")
NODE = shutil.which("node")

M5 = 300_000
T = 1_792_300_200_000          # a 5-minute boundary; trades are created at a bar edge, as on the desk
DAY_MS = 86_400_000


def js(fn, *args):
    proc = subprocess.run([NODE, HARNESS], input=json.dumps({"fn": fn, "args": list(args)}), capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def bar(i, o, h, l, c):
    return {"start": T + i * M5, "end": T + (i + 1) * M5, "o": o, "h": h, "l": l, "c": c}


def trade(side, entry, stop, tp1, tp2, **kw):
    return {"side": side, "entry": entry, "stop": stop, "risk": abs(entry - stop), "tp1": tp1, "tp2": tp2, "created": T,
            "style": "day", "status": "open", "tp1Hit": False, "tp1At": None, **kw}


def resolve(t, bars, now=None, timeout_ms=DAY_MS):
    now = bars[-1]["end"] if now is None else now
    return js("resolveTrade", t, bars, {"now": now, "timeoutMs": timeout_ms})


@unittest.skipUnless(NODE, "node not installed")
class Resolver(unittest.TestCase):
    def test_reviewer_case_tp1_and_tp2_in_one_bar_is_tp2(self):
        # REVIEW.md A2: buy 100, stop 90, TP1 110, TP2 120; the first bar runs to 125 with its low above entry
        t = trade("buy", 100, 90, 110, 120)
        out = resolve(t, [bar(0, 101, 125, 101, 121), bar(1, 121, 109, 99, 100)])
        self.assertTrue(out["final"])
        p = out["patch"]
        self.assertEqual((p["status"], p["exit"], p["closedAt"], p["tp1Hit"]), ("tp2", 120, T + M5, True))
        self.assertAlmostEqual(p["r"], 1.5)
        # the diagnostic's bars carry no open price: same answer
        p = resolve(t, [bar(0, None, 125, 101, 121), bar(1, None, 109, 99, 100)])["patch"]
        self.assertEqual(p["status"], "tp2")
        self.assertAlmostEqual(p["r"], 1.5)

    def test_sell_mirror_of_reviewer_case(self):
        out = resolve(trade("sell", 100, 110, 90, 80), [bar(0, 99, 99, 75, 79)])
        self.assertEqual(out["patch"]["status"], "tp2")
        self.assertAlmostEqual(out["patch"]["r"], 1.5)

    def test_stop_and_target_in_one_bar_is_ambiguous(self):
        t = trade("buy", 100, 90, 115, 130)
        p = resolve(t, [bar(0, 100, 116, 89, 95)])["patch"]
        self.assertEqual((p["status"], p["r"], p["exit"]), ("ambiguous", None, None))
        self.assertAlmostEqual(p["rCons"], -1.0)                       # stop first
        self.assertAlmostEqual(p["rOpt"], 0.75)                        # TP1 first, then back through entry
        p = resolve(t, [bar(0, 100, 131, 89, 95)])["patch"]          # the bar also reached TP2
        self.assertEqual(p["status"], "ambiguous")
        self.assertAlmostEqual(p["rOpt"], 2.25)

    def test_ambiguity_after_tp1_keeps_the_optimistic_branch_running(self):
        t = trade("buy", 100, 90, 115, 130)
        b0 = bar(0, 101, 116, 99.5, 105)                               # TP1 crossed; the dip to 99.5 may have come before it
        first = resolve(t, [b0])
        self.assertFalse(first["final"])
        self.assertTrue(first["material"])
        self.assertEqual(first["patch"]["sim"]["cons"]["done"]["status"], "be")
        self.assertIsNone(first["patch"]["sim"]["opt"]["done"])
        p = resolve(t, [b0, bar(1, 105, 131, 104, 129)])["patch"]
        self.assertEqual(p["status"], "ambiguous")
        self.assertAlmostEqual(p["rCons"], 0.75)
        self.assertAlmostEqual(p["rOpt"], 2.25)

    def test_close_below_entry_after_tp1_is_a_sure_breakeven(self):
        p = resolve(trade("buy", 100, 90, 115, 130), [bar(0, 101, 116, 99, 99.5)])["patch"]
        self.assertEqual(p["status"], "be")                            # after TP1 price ended under entry: BE was hit
        self.assertAlmostEqual(p["r"], 0.75)

    def test_gap_through_stop_exits_at_the_open(self):
        t = trade("buy", 100, 90, 115, 130)
        p = resolve(t, [bar(0, 101, 103, 95, 96), bar(1, 85, 86, 80, 82)])["patch"]
        self.assertEqual((p["status"], p["exit"], p["closedAt"]), ("sl", 85, T + 2 * M5))
        self.assertAlmostEqual(p["r"], -1.5)
        p = resolve(trade("sell", 100, 110, 85, 70), [bar(0, 114, 115, 113, 114)])["patch"]
        self.assertEqual((p["status"], p["exit"]), ("sl", 114))
        self.assertAlmostEqual(p["r"], -1.4)

    def test_gap_through_moved_stop_after_tp1(self):
        t = trade("buy", 100, 90, 115, 130)
        p = resolve(t, [bar(0, 101, 116, 101, 114), bar(1, 97, 98, 96, 97)])["patch"]
        self.assertEqual((p["status"], p["exit"]), ("be", 97))
        self.assertAlmostEqual(p["r"], 0.75 - 0.15)

    def test_unclosed_candle_never_finalizes(self):
        t = trade("buy", 100, 90, 115, 130)
        bars = [bar(0, 99, 101, 89, 90)]                               # touches the stop, but still forming
        out = resolve(t, bars, now=T + M5 - 1)
        self.assertFalse(out["final"])
        self.assertFalse(out["material"])
        self.assertNotIn("status", out["patch"])
        out = resolve(t, bars, now=T + M5)
        self.assertEqual((out["final"], out["patch"]["status"], out["patch"]["r"]), (True, "sl", -1))

    def test_breakeven_after_tp1_is_half_r_of_tp1(self):
        t = trade("buy", 100, 90, 115, 130)                            # TP1 at 1.5R
        b0, b1 = bar(0, 101, 116, 101, 114), bar(1, 113, 114, 99, 100)
        mid = resolve(t, [b0])
        self.assertEqual((mid["final"], mid["material"], mid["patch"]["tp1Hit"], mid["patch"]["tp1At"]), (False, True, True, T + M5))
        p = resolve(t, [b0, b1])["patch"]
        self.assertEqual((p["status"], p["exit"], p["closedAt"]), ("be", 100, T + 2 * M5))
        self.assertAlmostEqual(p["r"], 0.75)

    def test_resume_from_saved_walk_when_history_moved_on(self):
        t = trade("buy", 100, 90, 115, 130)
        mid = resolve(t, [bar(0, 101, 116, 101, 114)])
        saved = {**t, **mid["patch"]}
        p = resolve(saved, [bar(1, 113, 114, 99, 100)])["patch"]    # bar 0 is no longer in the history
        self.assertEqual(p["status"], "be")
        self.assertAlmostEqual(p["r"], 0.75)
        p = resolve(t, [bar(5, 101, 102, 100, 101)])["patch"]        # no saved walk and the history starts too late
        self.assertEqual(p["status"], "nodata")

    def test_timeout_exits_at_last_close_before_the_limit(self):
        t = trade("buy", 100, 90, 115, 130)
        bars = [bar(0, 100, 102, 99, 101), bar(1, 101, 103, 100, 102), bar(2, 102, 104, 101, 103), bar(3, 103, 140, 80, 104)]
        early = resolve(t, bars[:2], timeout_ms=3 * M5)
        self.assertFalse(early["final"])
        p = resolve(t, bars, timeout_ms=3 * M5)["patch"]             # bar 3 starts at the limit and is ignored
        self.assertEqual((p["status"], p["exit"], p["closedAt"]), ("timeout", 103, T + 3 * M5))
        self.assertAlmostEqual(p["r"], 0.3)
        bars = [bar(0, 101, 116, 101, 114), bar(1, 114, 118, 112, 117), bar(2, 117, 119, 116, 118)]
        p = resolve(t, bars, timeout_ms=3 * M5)["patch"]
        self.assertEqual(p["status"], "timeout")
        self.assertAlmostEqual(p["r"], 0.75 + 0.5 * 1.8)

    def test_stored_plan_with_target_on_the_loss_side_is_invalid(self):
        p = resolve(trade("buy", 100, 90, 95, 130), [bar(0, 100, 101, 99, 100)])["patch"]
        self.assertEqual((p["status"], p["r"]), ("invalid", None))


@unittest.skipUnless(NODE, "node not installed")
class Entry(unittest.TestCase):
    PLAN = {"side": "buy", "zoneLo": 99, "zoneHi": 100, "stop": 95, "tp1": 108, "tp2": 115, "touchedAt": T + 60_000}

    def candle(self, i, c):
        return {"start": T + i * 900_000, "end": T + (i + 1) * 900_000, "h": c, "l": c, "c": c}

    def test_confirms_on_close_back_above_zone(self):
        out = js("confirmEntry", self.PLAN, [self.candle(0, 99.5), self.candle(1, 101)])
        self.assertEqual((out["ok"], out["entry"], out["at"]), (True, 101, T + 2 * 900_000))
        self.assertAlmostEqual(out["risk"], 6)
        self.assertAlmostEqual(out["r1"], 7 / 6)

    def test_tp1_on_wrong_side_is_rejected(self):
        out = js("confirmEntry", {**self.PLAN, "tp1": 102, "tp2": 115}, [self.candle(0, 103)])  # closed beyond TP1
        self.assertEqual((out["ok"], out["reason"]), (False, "tp1_wrong_side"))

    def test_tp1_under_one_r_is_rejected(self):
        out = js("confirmEntry", {**self.PLAN, "tp1": 104}, [self.candle(0, 101)])            # 3 / 6 = 0.5R
        self.assertEqual((out["ok"], out["reason"]), (False, "tp1_under_min_r"))

    def test_sell_uses_signed_distances(self):
        plan = {"side": "sell", "zoneLo": 100, "zoneHi": 101, "stop": 105, "tp1": 92, "tp2": 85, "touchedAt": T}
        self.assertTrue(js("confirmEntry", plan, [self.candle(0, 99)])["ok"])
        out = js("confirmEntry", {**plan, "tp1": 99.5}, [self.candle(0, 99)])                # TP1 above a sell fill
        self.assertEqual(out["reason"], "tp1_wrong_side")

    def test_gives_up_after_max_confirmation_candles(self):
        n = js("RULES")["maxConfirm"]
        waiting = js("confirmEntry", self.PLAN, [self.candle(i, 99.5) for i in range(n - 1)])
        self.assertEqual(waiting, {"done": False})
        out = js("confirmEntry", self.PLAN, [self.candle(i, 99.5) for i in range(n)] + [self.candle(n, 101)])
        self.assertEqual((out["done"], out["ok"], out["reason"]), (True, False, "no_confirm"))

    def test_candles_only_from_bars_closed_when_fetched(self):
        b = T - T % 900_000                                        # a 15-minute boundary
        bars = [{"t": (b + i * M5) // 1000, "h": 1 + i, "l": 1, "c": 1 + i} for i in range(4)]  # 4 x 5m bars
        cs = js("candlesOf", bars, 900, b + 3 * M5 + 10_000)       # fetched while bar 3 (next candle) was forming
        self.assertEqual([(c["start"], c["end"], c["c"], c["h"]) for c in cs], [(b, b + 900_000, 3, 3)])
        self.assertEqual(js("candlesOf", bars, 900, b + 3 * M5 - 1), [])

    def test_news_unavailable_is_not_no_news(self):
        cal = [{"ts": T + 3_600_000, "importance": 1, "title": "CPI"}]
        cover = {"loaded": True, "from": T - DAY_MS, "to": T + 2 * DAY_MS}
        self.assertEqual(js("newsStatus", cal, {"loaded": False}, T)["status"], "unavailable")
        self.assertEqual(js("newsStatus", [], {**cover, "to": T + 60_000}, T)["status"], "unavailable")  # outside the fetched range
        self.assertEqual(js("newsStatus", cal, cover, T)["status"], "ok")
        self.assertEqual(js("newsStatus", cal, cover, T + 3_600_000 - 20 * 60_000), {"status": "news", "title": "CPI", "at": T + 3_600_000})

    def test_plan_text_states_only_what_is_checked(self):
        f = {"entry": "100.0", "zoneLo": "99.0", "zoneHi": "100.0", "stop": "95.0"}
        rules = js("RULES")
        for side in ("buy", "sell"):
            text = js("confirmText", "day", side, f) + js("cancelText", "day", side, f)
            for claim in ("CVD", "กำแพง", "ครึ่งไม้"):
                self.assertNotIn(claim, text)
            for fact in (rules["styles"]["day"]["conf"], f"ภายใน {rules['maxConfirm']} แท่ง", f"≥ {rules['minTp1R']}R", "ไม่ทราบข่าว", "24 ชม."):
                self.assertIn(fact, text)


@unittest.skipUnless(NODE, "node not installed")
class Stats(unittest.TestCase):
    def test_headline_excludes_ambiguous_open_and_other_versions(self):
        v = js("RULES")["version"]
        recs = [{"rule_version": v, "status": "tp2", "r": 1.5, "tp1Hit": True},
                {"rule_version": v, "status": "sl", "r": -1, "tp1Hit": False},
                {"rule_version": v, "status": "be", "r": 0.75, "tp1Hit": True, "conditions": {"news": "unavailable"}},
                {"rule_version": v, "status": "ambiguous", "r": None, "rCons": -1, "rOpt": 0.75, "tp1Hit": False},
                {"rule_version": v, "status": "open", "r": None},
                {"rule_version": v, "status": "nodata", "r": None},
                {"status": "tp2", "r": 1.5, "tp1Hit": True}]                       # resolved by an older rule set
        s = js("stats", recs)
        self.assertEqual((s["n"], s["tp1"], s["open"], s["ambiguous"], s["nodata"], s["otherVersions"], s["newsUnavailable"]),
                         (3, 2, 1, 1, 1, 1, 1))
        self.assertAlmostEqual(s["sum"], 1.25)
        self.assertAlmostEqual(s["tp1Rate"], 2 / 3)
        self.assertEqual((s["ambCons"], s["ambOpt"]), (-1, 0.75))
        self.assertIsNone(js("stats", [])["avg"])


if __name__ == "__main__":
    unittest.main()
