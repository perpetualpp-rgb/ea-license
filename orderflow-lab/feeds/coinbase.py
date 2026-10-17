"""Coinbase Exchange public REST feed. No API key needed.

Docs: https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductbook
* ``/products/{id}/book?level=2`` full aggregated book (can be ~1 MB for
  BTC-USD, so callers usually truncate).
* ``/products/{id}/trades`` recent trades, newest first.

IMPORTANT: Coinbase's trade ``side`` is the **maker** order side ("If the side
is sell this indicates the maker was a sell order and the match is considered
an up-tick"). We invert it so ``Trade.side`` is the taker side like every
other feed; getting this wrong flips the sign of CVD.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from .http import get_json
from .types import BookSnapshot, Trade, sort_book

BASE = "https://api.exchange.coinbase.com"
NAME = "coinbase"


class CoinbaseFeed:
    name = NAME

    def __init__(self, symbol: str):
        self.symbol = symbol  # e.g. "BTC-USD", "PAXG-USD"

    def fetch_book(self, depth: int = 400) -> BookSnapshot:
        payload = get_json(f"{BASE}/products/{self.symbol}/book", {"level": 2}, timeout=20)
        return parse_book(payload, self.symbol).truncated(depth)

    def fetch_trades(self, limit: int = 1000) -> List[Trade]:
        payload = get_json(f"{BASE}/products/{self.symbol}/trades", {"limit": min(limit, 1000)})
        return parse_trades(payload, self.symbol)


def _iso_ms(s: str) -> int:
    """Parse ISO-8601 UTC with any fraction length (Coinbase sends nanoseconds)."""
    s = s.rstrip("Z")
    head, _, frac = s.partition(".")
    dt = datetime.fromisoformat(head).replace(tzinfo=timezone.utc)
    ms = int((frac + "000")[:3]) if frac else 0
    return int(dt.timestamp()) * 1000 + ms


def parse_book(payload: dict, symbol: str) -> BookSnapshot:
    ts = _iso_ms(payload["time"]) if payload.get("time") else 0
    bids, asks = sort_book(((float(p), float(s)) for p, s, *_ in payload["bids"]),
                           ((float(p), float(s)) for p, s, *_ in payload["asks"]))
    return BookSnapshot(ts, NAME, symbol, bids, asks)


def parse_trades(payload: list, symbol: str) -> List[Trade]:
    trades = [Trade(_iso_ms(d["time"]), float(d["price"]), float(d["size"]),
                    "buy" if d["side"] == "sell" else "sell",  # maker side -> taker side
                    NAME, symbol, str(d["trade_id"])) for d in payload]
    trades.sort(key=lambda t: (t.ts, int(t.trade_id)))
    return trades
