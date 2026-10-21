"""OKX public REST feed (spot and perpetual swaps). No API key needed.

Docs: https://www.okx.com/docs-v5/en/#order-book-trading-market-data
* ``/market/books``   depth up to 400 levels per side.
* ``/market/trades``  up to 500 most recent trades; ``side`` is the taker side.

Swap contracts report ``sz`` in contracts; pass ``contract_value`` (e.g. 0.01
BTC for BTC-USDT-SWAP) to convert sizes to base units.
"""
from __future__ import annotations

from typing import List

from .http import get_json
from .types import BookSnapshot, Trade, sort_book

BASE = "https://www.okx.com/api/v5"
NAME = "okx"


class OKXFeed:
    name = NAME

    def __init__(self, symbol: str, contract_value: float = 1.0):
        self.symbol = symbol  # e.g. "BTC-USDT", "XAUT-USDT", "BTC-USDT-SWAP"
        self.contract_value = contract_value

    def fetch_book(self, depth: int = 400) -> BookSnapshot:
        payload = get_json(f"{BASE}/market/books", {"instId": self.symbol, "sz": min(depth, 400)})
        return parse_book(payload, self.symbol, self.contract_value)

    def fetch_trades(self, limit: int = 500) -> List[Trade]:
        payload = get_json(f"{BASE}/market/trades", {"instId": self.symbol, "limit": min(limit, 500)})
        return parse_trades(payload, self.symbol, self.contract_value)


def _check(payload: dict) -> list:
    if str(payload.get("code")) != "0":
        raise ValueError(f"okx error {payload.get('code')}: {payload.get('msg')}")
    return payload["data"]


def parse_book(payload: dict, symbol: str, contract_value: float = 1.0) -> BookSnapshot:
    book = _check(payload)[0]
    # level format: [price, size, deprecated, order_count]
    bids, asks = sort_book(((float(p), float(s) * contract_value) for p, s, *_ in book["bids"]),
                           ((float(p), float(s) * contract_value) for p, s, *_ in book["asks"]))
    return BookSnapshot(int(book["ts"]), NAME, symbol, bids, asks)


def parse_trades(payload: dict, symbol: str, contract_value: float = 1.0) -> List[Trade]:
    trades = [Trade(int(d["ts"]), float(d["px"]), float(d["sz"]) * contract_value, d["side"],
                    NAME, symbol, str(d["tradeId"])) for d in _check(payload)]
    trades.sort(key=lambda t: (t.ts, int(t.trade_id)))
    return trades
