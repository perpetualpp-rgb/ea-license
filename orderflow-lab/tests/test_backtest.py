"""Desk plan back-test (desk/backtest.js over desk/plan.js + desk/journal.js) on synthetic 5-minute candles.
Skipped when node is not installed."""
import json
import os
import shutil
import subprocess
import unittest

DESK = os.path.join(os.path.dirname(__file__), "..", "desk")
NODE = shutil.which("node")

# 4,000 five-minute candles (about two weeks) of a mean-reverting walk near 4,000, with taker-buy volume;
# replayed whole and cut at candle 3,000, so every trade made before the cut can be compared
SCRIPT = r"""
const { replay, summarize, INS } = require(process.argv[1] + "/backtest.js"), OFP = require(process.argv[1] + "/plan.js");
let s = 7; const r = () => (s = (s * 16807) % 2147483647) / 2147483647;
const bars = []; let p = 4000; const t0 = 1790000000 - (1790000000 % 86400);
for (let i = 0; i < 4000; i++) {
  const o = p, c = o + (r() - 0.5) * 6 + (4000 - o) * 0.01, h = Math.max(o, c) + r() * 2, l = Math.min(o, c) - r() * 2, v = 50 + r() * 200;
  bars.push({ t: t0 + i * 300, o, h, l, c, v, bv: v * (0.3 + r() * 0.4) }); p = c;
}
const full = replay(bars, [], INS.xau), cut = replay(bars.slice(0, 3000), [], INS.xau), cutEnd = (bars[2999].t + 300) * 1000;
const key = t => [t.style, t.side, t.created, t.entry, t.stop, t.tp1, t.tp2].join("|");
const closeAt = new Map(bars.map(b => [(b.t + 300) * 1000, b.c]));
console.log(JSON.stringify({
  n: full.trades.length, funnel: full.funnel,
  before: full.trades.filter(t => t.created <= cutEnd).map(key), cut: cut.trades.map(key),
  trades: full.trades.map(t => ({ style: t.style, status: t.status, r: t.r, created: t.created, entry: t.entry, close: closeAt.get(t.created),
                                  conf: OFP.PLAN[t.style].confSec, dir: t.side === "sell" ? -1 : 1, stop: t.stop, tp1: t.tp1, tp2: t.tp2 })),
  summary: summarize(full.trades, 0.001).core,
}));
"""


@unittest.skipUnless(NODE, "node not installed")
class Backtest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run([NODE, "-e", SCRIPT, os.path.abspath(DESK)], capture_output=True, text=True, check=True).stdout
        cls.res = json.loads(out)

    def test_makes_trades(self):
        self.assertGreater(self.res["n"], 5, self.res["funnel"])

    def test_no_look_ahead(self):
        # a trade is decided from the candles up to its time only: cutting the data later changes none of them
        self.assertEqual(self.res["before"], self.res["cut"])

    def test_entry_is_the_confirmation_close(self):
        for t in self.res["trades"]:
            self.assertEqual((t["created"] // 1000) % t["conf"], 0, t)      # a confirmation candle ends there
            self.assertEqual(t["entry"], t["close"], t)                    # filled at that candle's close
            self.assertGreater((t["entry"] - t["stop"]) * t["dir"], 0, t)
            self.assertGreater((t["tp2"] - t["tp1"]) * t["dir"], 0, t)

    def test_outcomes_follow_the_rules(self):
        for t in self.res["trades"]:
            if t["status"] == "sl":
                self.assertLessEqual(t["r"], -1 + 1e-9, t)
            if t["status"] in ("tp2", "be"):
                self.assertGreater(t["r"], 0, t)
        s = self.res["summary"]
        self.assertAlmostEqual(s["gross"]["sum"] - s["net"]["sum"], s["costR"] * s["n"], places=6)


if __name__ == "__main__":
    unittest.main()
