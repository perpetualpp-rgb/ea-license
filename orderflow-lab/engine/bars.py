"""Aggregate trades into OHLCV bars so bar-based tools (VWAP, profile) work on
exchange tick data exactly as they do on TradingView bars."""
from __future__ import annotations

from typing import Iterable, List

from feeds.types import Bar, Trade


def bucket_start(ts_ms: int, interval_ms: int) -> int:
    return ts_ms - ts_ms % interval_ms


def bars_from_trades(trades: Iterable[Trade], interval_ms: int) -> List[Bar]:
    """Build bars (``t`` in unix seconds) from trades sorted by time.
    Empty intervals are skipped, matching how exchanges report candles."""
    bars: List[Bar] = []
    cur = None
    for tr in trades:
        b0 = bucket_start(tr.ts, interval_ms)
        if cur is None or b0 != cur["t"]:
            if cur is not None:
                bars.append(Bar(cur["t"] // 1000, cur["o"], cur["h"], cur["l"], cur["c"], cur["v"]))
            cur = {"t": b0, "o": tr.price, "h": tr.price, "l": tr.price, "c": tr.price, "v": 0.0}
        cur["h"] = max(cur["h"], tr.price)
        cur["l"] = min(cur["l"], tr.price)
        cur["c"] = tr.price
        cur["v"] += tr.size
    if cur is not None:
        bars.append(Bar(cur["t"] // 1000, cur["o"], cur["h"], cur["l"], cur["c"], cur["v"]))
    return bars
