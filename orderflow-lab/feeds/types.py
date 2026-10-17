"""Normalised market-data records shared by every feed and engine module.

Everything downstream works on these three shapes only, so adding an exchange
means writing one parser, not touching the engine.

Conventions
* ``Trade.side`` is always the **taker** (aggressor) side: ``"buy"`` means a
  market buy hit the ask. Exchanges that report the maker side (Coinbase) are
  inverted in their parser.
* Prices/sizes are floats in the venue's native units (BTC, PAXG, contracts).
* ``ts`` is unix milliseconds; ``Bar.t`` is unix seconds (TradingView style).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Iterable, Iterator, List, Tuple

Level = Tuple[float, float]  # (price, size)


@dataclass(frozen=True)
class Trade:
    ts: int
    price: float
    size: float
    side: str  # "buy" | "sell" (taker side)
    exchange: str
    symbol: str
    trade_id: str

    @property
    def signed_size(self) -> float:
        return self.size if self.side == "buy" else -self.size


@dataclass(frozen=True)
class BookSnapshot:
    ts: int
    exchange: str
    symbol: str
    bids: Tuple[Level, ...]  # best bid first (descending price)
    asks: Tuple[Level, ...]  # best ask first (ascending price)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.bids and self.asks:
            return (self.bids[0][0] + self.asks[0][0]) / 2.0
        return self.best_bid or self.best_ask

    def truncated(self, depth: int) -> "BookSnapshot":
        """Keep only the ``depth`` levels nearest the touch on each side."""
        return BookSnapshot(self.ts, self.exchange, self.symbol,
                            self.bids[:depth], self.asks[:depth])


@dataclass(frozen=True)
class Bar:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float

    @property
    def typical(self) -> float:
        return (self.h + self.l + self.c) / 3.0


def sort_book(bids: Iterable[Level], asks: Iterable[Level]) -> Tuple[Tuple[Level, ...], Tuple[Level, ...]]:
    """Return (bids desc, asks asc) with zero-size levels dropped."""
    b = tuple(sorted(((p, s) for p, s in bids if s > 0), key=lambda x: -x[0]))
    a = tuple(sorted(((p, s) for p, s in asks if s > 0), key=lambda x: x[0]))
    return b, a


# --- JSONL persistence -------------------------------------------------------

def to_json(obj) -> str:
    d = asdict(obj)
    if isinstance(obj, BookSnapshot):
        d["bids"] = [list(x) for x in obj.bids]
        d["asks"] = [list(x) for x in obj.asks]
    return json.dumps(d, separators=(",", ":"))


def _iter_jsonl(path: str) -> Iterator[dict]:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if "_meta" in d:  # header line, see data/samples
                continue
            yield d


def read_bars(path: str) -> List[Bar]:
    return [Bar(int(d["t"]), float(d["o"]), float(d["h"]), float(d["l"]), float(d["c"]), float(d["v"]))
            for d in _iter_jsonl(path)]


def read_trades(path: str) -> List[Trade]:
    return [Trade(int(d["ts"]), float(d["price"]), float(d["size"]), d["side"], d["exchange"], d["symbol"], str(d["trade_id"]))
            for d in _iter_jsonl(path)]


def read_books(path: str) -> List[BookSnapshot]:
    out = []
    for d in _iter_jsonl(path):
        bids, asks = sort_book(((float(p), float(s)) for p, s in d["bids"]),
                               ((float(p), float(s)) for p, s in d["asks"]))
        out.append(BookSnapshot(int(d["ts"]), d["exchange"], d["symbol"], bids, asks))
    return out
