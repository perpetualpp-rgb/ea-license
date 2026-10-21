"""Kraken public REST feed. No API key needed.

Docs: https://docs.kraken.com/api/docs/rest-api/get-order-book
* ``Depth``  up to 500 levels per side; level = [price, volume, timestamp].
* ``Trades`` up to 1000 trades since a cursor; row =
  [price, volume, time, side(b/s), type(m/l), misc, trade_id]. ``side`` is the
  taker side. The response ``last`` field is the cursor for the next poll.

Kraken answers with its internal pair name (``XBTUSD`` -> ``XXBTZUSD``), so
parsers take the first key of ``result`` instead of echoing the request.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .http import get_json
from .types import BookSnapshot, Trade, sort_book

BASE = "https://api.kraken.com/0/public"
NAME = "kraken"


class KrakenFeed:
    name = NAME

    def __init__(self, symbol: str):
        self.symbol = symbol  # e.g. "XBTUSD", "PAXGUSD"
        self.cursor: Optional[str] = None

    def fetch_book(self, depth: int = 500) -> BookSnapshot:
        payload = get_json(f"{BASE}/Depth", {"pair": self.symbol, "count": min(depth, 500)})
        return parse_book(payload, self.symbol)

    def fetch_trades(self, limit: int = 1000) -> List[Trade]:
        params = {"pair": self.symbol, "count": min(limit, 1000)}
        if self.cursor:
            params["since"] = self.cursor
        payload = get_json(f"{BASE}/Trades", params)
        trades, self.cursor = parse_trades(payload, self.symbol)
        return trades


def _result(payload: dict) -> dict:
    if payload.get("error"):
        raise ValueError(f"kraken error: {payload['error']}")
    return payload["result"]


def parse_book(payload: dict, symbol: str) -> BookSnapshot:
    result = _result(payload)
    book = next(iter(result.values()))
    ts = max((int(lvl[2]) for lvl in book["bids"] + book["asks"]), default=0) * 1000
    bids, asks = sort_book(((float(p), float(s)) for p, s, *_ in book["bids"]),
                           ((float(p), float(s)) for p, s, *_ in book["asks"]))
    return BookSnapshot(ts, NAME, symbol, bids, asks)


def parse_trades(payload: dict, symbol: str) -> Tuple[List[Trade], Optional[str]]:
    result = _result(payload)
    cursor = result.get("last")
    rows = next((v for k, v in result.items() if k != "last"), [])
    trades = []
    for row in rows:
        price, vol, t, side = row[0], row[1], row[2], row[3]
        trade_id = str(row[6]) if len(row) > 6 else f"{int(float(t) * 1e6)}"
        trades.append(Trade(int(float(t) * 1000), float(price), float(vol),
                            "buy" if side == "b" else "sell", NAME, symbol, trade_id))
    trades.sort(key=lambda x: (x.ts, x.trade_id))
    return trades, cursor
