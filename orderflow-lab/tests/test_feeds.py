"""Parser tests against real (recorded) exchange responses in tests/fixtures."""
import json
import os
import unittest

from feeds import coinbase, kraken, okx
from feeds.registry import make_feed
from feeds.types import BookSnapshot, Trade, read_books, read_trades, to_json

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


class OKXParsers(unittest.TestCase):
    def test_book_sorted_and_sized(self):
        book = okx.parse_book(load("okx_book_btc.json"), "BTC-USDT")
        self.assertEqual(book.exchange, "okx")
        self.assertEqual(len(book.bids), 5)
        self.assertEqual(len(book.asks), 5)
        self.assertGreater(book.asks[0][0], book.bids[0][0])
        self.assertEqual(book.bids, tuple(sorted(book.bids, key=lambda x: -x[0])))
        self.assertEqual(book.asks, tuple(sorted(book.asks, key=lambda x: x[0])))
        self.assertGreater(book.ts, 1_700_000_000_000)

    def test_trades_taker_side_and_order(self):
        trades = okx.parse_trades(load("okx_trades_btc.json"), "BTC-USDT")
        self.assertEqual(len(trades), 5)
        self.assertTrue(all(t.side in ("buy", "sell") for t in trades))
        self.assertEqual([t.ts for t in trades], sorted(t.ts for t in trades))
        # fixture row: tradeId 1071634443 px 81262 sz 0.000011 side sell
        last = [t for t in trades if t.trade_id == "1071634443"][0]
        self.assertEqual((last.price, last.size, last.side), (81262.0, 0.000011, "sell"))
        self.assertEqual(last.signed_size, -0.000011)

    def test_contract_value_scales_swap_sizes(self):
        trades = okx.parse_trades(load("okx_trades_btc.json"), "BTC-USDT-SWAP", contract_value=0.01)
        raw = okx.parse_trades(load("okx_trades_btc.json"), "BTC-USDT-SWAP")
        self.assertAlmostEqual(trades[0].size, raw[0].size * 0.01)

    def test_error_payload_raises(self):
        with self.assertRaises(ValueError):
            okx.parse_trades({"code": "51001", "msg": "Instrument ID does not exist.", "data": []}, "X")


class KrakenParsers(unittest.TestCase):
    def test_book_uses_internal_pair_key(self):
        book = kraken.parse_book(load("kraken_book_btc.json"), "XBTUSD")
        self.assertEqual(book.symbol, "XBTUSD")
        self.assertEqual(len(book.bids), 5)
        self.assertGreater(book.asks[0][0], book.bids[0][0])
        self.assertGreater(book.ts, 1_700_000_000_000)

    def test_trades_side_letters_and_cursor(self):
        trades, cursor = kraken.parse_trades(load("kraken_trades_btc.json"), "XBTUSD")
        self.assertEqual(cursor, "1792254998101686448")
        self.assertEqual(len(trades), 5)
        # row: ["81142.50000","0.00437970",1792254994.3502982,"s","l","",111848796]
        first = trades[0]
        self.assertEqual((first.price, first.side, first.trade_id), (81142.5, "sell", "111848796"))
        self.assertEqual(first.ts, 1792254994350)
        self.assertEqual([t.side for t in trades], ["sell", "buy", "buy", "sell", "buy"])

    def test_error_raises(self):
        with self.assertRaises(ValueError):
            kraken.parse_book({"error": ["EQuery:Unknown asset pair"]}, "NOPE")


class CoinbaseParsers(unittest.TestCase):
    def test_book_parses_nanosecond_time(self):
        book = coinbase.parse_book(load("coinbase_book_btc.json"), "BTC-USD")
        self.assertEqual(book.ts, 1792255004999)  # 2026-10-17T16:36:44.999847368Z
        self.assertEqual(len(book.bids), 5)
        self.assertGreater(book.asks[0][0], book.bids[0][0])
        self.assertEqual(len(book.truncated(2).asks), 2)

    def test_trades_maker_side_is_inverted(self):
        trades = coinbase.parse_trades(load("coinbase_trades_btc.json"), "BTC-USD")
        self.assertEqual(len(trades), 5)
        # every fixture row has side "buy" (maker bought) -> taker sold
        self.assertTrue(all(t.side == "sell" for t in trades))
        self.assertEqual([t.ts for t in trades], sorted(t.ts for t in trades))
        self.assertEqual(trades[-1].trade_id, "1107405751")
        self.assertEqual(trades[-1].ts, 1792255006687)  # 16:36:46.687312Z


class RegistryAndJsonl(unittest.TestCase):
    def test_make_feed(self):
        self.assertIsInstance(make_feed({"exchange": "okx", "symbol": "BTC-USDT"}), okx.OKXFeed)
        self.assertIsInstance(make_feed({"exchange": "kraken", "symbol": "PAXGUSD"}), kraken.KrakenFeed)
        self.assertIsInstance(make_feed({"exchange": "coinbase", "symbol": "BTC-USD"}), coinbase.CoinbaseFeed)
        with self.assertRaises(ValueError):
            make_feed({"exchange": "binance", "symbol": "BTCUSDT"})

    def test_jsonl_round_trip(self):
        import tempfile
        t = Trade(1, 2.5, 0.1, "buy", "okx", "BTC-USDT", "7")
        b = BookSnapshot(2, "okx", "BTC-USDT", ((100.0, 1.0), (99.0, 2.0)), ((101.0, 3.0),))
        with tempfile.TemporaryDirectory() as d:
            tp, bp = os.path.join(d, "t.jsonl"), os.path.join(d, "b.jsonl")
            with open(tp, "w") as fh:
                fh.write(to_json(t) + "\n")
            with open(bp, "w") as fh:
                fh.write('{"_meta":{"x":1}}\n' + to_json(b) + "\n")
            self.assertEqual(read_trades(tp), [t])
            self.assertEqual(read_books(bp), [b])


if __name__ == "__main__":
    unittest.main()
