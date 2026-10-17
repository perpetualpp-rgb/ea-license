"""The browser dashboard ships a JavaScript port of the engine (web/engine.js).
These tests run both implementations on identical input and compare numbers,
so the two cannot silently drift apart. Skipped when node is not installed."""
import json
import os
import random
import shutil
import subprocess
import unittest

from engine.summary import summarize_bars, summarize_trades
from feeds.types import BookSnapshot, Trade
from tests.synth import synthetic_bars

HERE = os.path.dirname(__file__)
HARNESS = os.path.join(HERE, "js_parity.js")
NODE = shutil.which("node")


def run_js(payload: dict) -> dict:
    proc = subprocess.run([NODE, HARNESS], input=json.dumps(payload), capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def synthetic(seed: int = 7):
    """Deterministic trades + books with a persistent bid wall and a big print."""
    rng = random.Random(seed)
    t0, price, trades = 1_700_000_000_000, 100.0, []
    for i in range(600):
        price = round(max(90.0, price * (1 + rng.gauss(0, 0.0004))), 2)
        size = round(rng.lognormvariate(-2.0, 0.8), 4)
        if i == 300:
            size = 25.0
        trades.append(Trade(t0 + i * 1_000, price, size, "buy" if rng.random() < 0.52 else "sell", "x", "S", str(i)))
    books = []
    for k in range(12):
        ts = t0 + k * 50_000
        mid = trades[min(len(trades) - 1, k * 50)].price
        bids = [(round(mid - 0.01 * (j + 1), 2), round(0.5 + rng.random(), 4)) for j in range(60)]
        asks = [(round(mid + 0.01 * (j + 1), 2), round(0.5 + rng.random(), 4)) for j in range(60)]
        if k < 9:
            bids[20] = (bids[20][0], 40.0 + k)  # wall grows, then disappears before price reaches it
        if 3 <= k <= 7:
            asks[10] = (asks[10][0], 30.0)
        books.append(BookSnapshot(ts, "x", "S", tuple(bids), tuple(asks)))
    return trades, books


def trade_json(t: Trade) -> dict:
    return {"ts": t.ts, "price": t.price, "size": t.size, "side": t.side, "exchange": t.exchange, "symbol": t.symbol, "trade_id": t.trade_id}


def book_json(b: BookSnapshot) -> dict:
    return {"ts": b.ts, "exchange": b.exchange, "symbol": b.symbol, "bids": [list(x) for x in b.bids], "asks": [list(x) for x in b.asks]}


@unittest.skipUnless(NODE, "node not installed")
class JsParity(unittest.TestCase):
    def assertClose(self, a, b, msg=""):
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
            self.assertAlmostEqual(a, b, places=6, msg=msg)
        elif isinstance(a, (tuple, list)):
            self.assertEqual(len(a), len(b), msg=msg)
            for x, y in zip(a, b):
                self.assertClose(x, y, msg)
        else:
            self.assertEqual(a, b, msg=msg)

    def test_bars_mode_on_synthetic_candles(self):
        bars = synthetic_bars()
        py = summarize_bars(bars, 0.1, 25)
        js = run_js({"mode": "bars", "bars": [vars(b) for b in bars], "tick": 0.1, "round_step": 25})
        for k in ("last", "volume", "bars", "t_first", "t_last", "round_levels", "nearest_round"):
            self.assertClose(py[k], js[k], k)
        for k in ("high", "low", "poc", "va_low", "va_high", "total"):
            self.assertClose(py["range"][k], js["range"][k], f"range.{k}")
        self.assertClose(py["magnet"]["low"], js["magnet"]["low"])
        self.assertClose(py["magnet"]["high"], js["magnet"]["high"])
        self.assertEqual([n["price"] for n in py["nodes"]["hvn"]], [n["price"] for n in js["nodes"]["hvn"]])
        self.assertEqual([n["price"] for n in py["nodes"]["lvn"]], [n["price"] for n in js["nodes"]["lvn"]])
        self.assertClose(py["vwap"]["vwap"], js["vwap"]["vwap"])
        self.assertClose(py["vwap"]["sd"], js["vwap"]["sd"])
        self.assertEqual(py["vwap"]["position"]["label"], js["vwap"]["position"]["label"])
        self.assertEqual(py["vwap"]["shape"], js["vwap"]["shape"])
        self.assertClose(py["cvd"]["value"], js["cvd"]["value"])
        self.assertEqual(py["cvd"]["divergence"], js["cvd"]["divergence"])

    def test_trades_mode_on_synthetic_flow(self):
        trades, books = synthetic()
        py = summarize_trades(trades, books, 0.1, 1.0, 60_000)
        js = run_js({"mode": "trades", "trades": [trade_json(t) for t in trades], "books": [book_json(b) for b in books],
                     "tick": 0.1, "round_step": 1.0, "interval_ms": 60_000})
        for k in ("trades", "last", "ts_first", "ts_last", "profile_buy", "profile_sell", "round_levels"):
            self.assertClose(py[k], js[k], k)
        for k in ("high", "low", "poc", "va_low", "va_high", "total"):
            self.assertClose(py["range"][k], js["range"][k], f"range.{k}")
        self.assertClose(py["cvd"]["value"], js["cvd"]["value"])
        self.assertEqual([(b["t"], round(b["delta"], 6)) for b in py["cvd"]["last_buckets"]],
                         [(b["t"], round(b["delta"], 6)) for b in js["cvd"]["last_buckets"]])
        self.assertClose(py["big_trades"]["threshold"], js["big_trades"]["threshold"])
        self.assertEqual(py["big_trades"]["count"], js["big_trades"]["count"])
        self.assertGreaterEqual(py["big_trades"]["count"], 1)
        self.assertEqual([(z["price"], round(z["buy"], 6), round(z["sell"], 6), z["contested"]) for z in py["big_trades"]["zones"]],
                         [(z["price"], round(z["buy"], 6), round(z["sell"], 6), z["contested"]) for z in js["big_trades"]["zones"]])
        # walls: the bid wall is pulled before price reaches it; the ask wall too
        self.assertEqual(py["walls"]["snapshots"], js["walls"]["snapshots"])
        key = lambda w: (w["side"], w["price"], round(w["size"], 6), w["status"], w["seen"], w["persistent"], round(w["growth"], 6))
        self.assertEqual([key(w) for w in py["walls"]["bid"]], [key(w) for w in js["walls"]["bid"]])
        self.assertEqual([key(w) for w in py["walls"]["ask"]], [key(w) for w in js["walls"]["ask"]])
        ekey = lambda e: (e["side"], e["price"], round(e["size"], 6), e["outcome"], e["age_ms"])
        self.assertEqual([ekey(e) for e in py["walls"]["events"]], [ekey(e) for e in js["walls"]["events"]])
        self.assertGreaterEqual(len(py["walls"]["events"]), 2)
        self.assertTrue({e["outcome"] for e in py["walls"]["events"]} <= {"pulled", "filled"})


if __name__ == "__main__":
    unittest.main()
