"""Order-flow checklist (desk/plan.js checklist) on a built sell set-up: CVD red, price up into the upper Assist
cloud and a sell zone with a standing ask wall, a red 5-minute candle off the zone, sellers on the tape, US open.
Skipped when node is not installed."""
import json
import os
import shutil
import subprocess
import unittest

DESK = os.path.join(os.path.dirname(__file__), "..", "desk")
NODE = shutil.which("node")

SCRIPT = r"""
const P = require(process.argv[1] + "/plan.js"), cases = JSON.parse(process.argv[2]);
const ins = { key: "xau", src: "bin", tick: 0.1, round: 25, sessOff: 22 * 3600 };
// now = 12:45 UTC = 19:45 Bangkok (US open); the session began at 22:00 UTC, the previous one a day earlier
const now = Date.UTC(2026, 9, 14, 12, 45) / 1000, start = Date.UTC(2026, 9, 13, 22, 0) / 1000;
let s = 3; const r = () => (s = (s * 16807) % 2147483647) / 2147483647;
const bars = [];
for (let t = start - 86400; t < now - 6 * 300; t += 300) {           // a quiet range 4170-4190, sellers ahead (CVD red)
  const o = 4180 + (r() - 0.5) * 16, c = o + (r() - 0.5) * 4, v = 100 + r() * 50;
  bars.push({ t, o, h: Math.max(o, c) + 1, l: Math.min(o, c) - 1, c, v, bv: v * 0.42 });
}
for (const [o, h, l, c] of [[4186, 4190, 4185, 4189], [4189, 4194, 4188, 4193], [4193, 4197, 4192, 4196], [4196, 4198.5, 4195, 4197.5], [4197.5, 4199, 4196, 4198], [4198, 4199, 4194, 4195]])
  bars.push({ t: bars[bars.length - 1].t + 300, o, h, l, c, v: 150, bv: 60 });   // the push up and the red candle off the zone
const out = {};
for (const [name, cfg] of Object.entries(cases)) {
  const ctx = { ins, D: P.derive(bars, ins), last: 4195, now: now * 1000, sess: bars, zone: { side: "sell", lo: 4197, hi: 4199, names: "test" },
    walls: [{ side: "ask", price: 4198, size: 50, persistent: true, status: cfg.wallStatus || "holding", age_ms: 600000 },
            { side: "bid", price: 4185, size: 30, persistent: true, status: "holding", age_ms: 900000 }],
    book: { asks: [[4198, 60]].concat(Array.from({ length: 199 }, (_, i) => [4198.5 + i * 0.5, 5 + (i % 3)])), bids: Array.from({ length: 200 }, (_, i) => [4194.5 - i * 0.5, 5 + (i % 3)]) },
    prevBook: { ask: new Map([[4198, 50]]), bid: new Map() }, bucket: 0.5, zoneTick: 1,
    bigZones: cfg.contested ? [{ price: 4198, buy: 30, sell: 35, contested: true }] : [], tape: { net5: -20, pressure: 40 },
    news: cfg.news ? { status: "news", title: "CPI" } : { status: "ok" } };
  if (cfg.cvdUp) ctx.D = P.derive(bars.map(b => ({ ...b, bv: b.v * 0.6 })), ins);
  if (cfg.expandUp) { const D = ctx.D, last = D.cur.slice(-12); for (const [i, b] of last.entries()) Object.assign(b, { h: b.h + i * 4, c: b.c + i * 4, o: b.o + i * 4, l: b.l + i * 4 }); }
  const res = P.checklist(ctx, { slBuf: 6 });
  out[name] = { ready: res.ready, side: res.side, plan: res.plan, items: Object.fromEntries(res.items.map(i => [i.id, [i.state, i.detail]])) };
}
console.log(JSON.stringify(out));
"""

CASES = {"base": {}, "contested": {"contested": True}, "pulling": {"wallStatus": "pulling"}, "news": {"news": True},
         "cvd_up": {"cvdUp": True}, "expand_up": {"expandUp": True}}


@unittest.skipUnless(NODE, "node not installed")
class Checklist(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run([NODE, "-e", SCRIPT, os.path.abspath(DESK), json.dumps(CASES)], capture_output=True, text=True, check=True).stdout
        cls.r = json.loads(out)

    def test_sell_setup_passes_and_orders(self):
        b = self.r["base"]
        self.assertEqual(b["side"], "sell", b["items"]["cvd"])
        for k in ("cvd", "cloud", "expand", "wall", "book", "bigx", "trigger", "flow", "sltp", "news"):
            self.assertEqual(b["items"][k][0], "pass", (k, b["items"][k]))
        self.assertTrue(b["ready"])
        p = b["plan"]
        self.assertEqual(p["entry"], 4195)                 # the trigger candle's close
        self.assertEqual(p["stop"], 4205)                  # above the zone and the candle's high (4199) by 6
        self.assertEqual(p["tp1"], 4185)                   # the first standing bid wall before VWAP
        self.assertLess(p["tp2"], p["tp1"])
        self.assertFalse(p["planned"])

    def test_fight_zone_blocks(self):
        c = self.r["contested"]
        self.assertEqual(c["items"]["bigx"][0], "fail")
        self.assertFalse(c["ready"])

    def test_pulled_wall_blocks(self):
        c = self.r["pulling"]
        self.assertEqual(c["items"]["wall"][0], "fail")
        self.assertIn("หด", c["items"]["wall"][1])
        self.assertFalse(c["ready"])

    def test_news_blocks(self):
        self.assertEqual(self.r["news"]["items"]["news"][0], "fail")
        self.assertFalse(self.r["news"]["ready"])

    def test_one_side_from_cvd(self):
        # CVD green: buys only, so the sell zone is not traded
        c = self.r["cvd_up"]
        self.assertEqual(c["side"], "buy")
        self.assertFalse(c["ready"])
        self.assertIsNone(c["plan"])

    def test_no_fade_into_an_expansion(self):
        c = self.r["expand_up"]
        self.assertEqual(c["items"]["expand"][0], "fail", c["items"]["expand"])
        self.assertFalse(c["ready"])


if __name__ == "__main__":
    unittest.main()
