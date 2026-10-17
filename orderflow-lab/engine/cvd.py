"""Cumulative Volume Delta and per-interval delta.

Exact when built from trades with a taker side. For bar-only feeds (candles
without a trade tape) ``approx_bar_delta`` estimates delta from where the close
sits inside the bar's range; treat that as an *estimate* and say so in any UI.
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

from feeds.types import Bar, Trade

from .bars import bucket_start


def cvd_series(trades: Iterable[Trade], start: float = 0.0) -> List[Tuple[int, float]]:
    """Running CVD after each trade: [(ts_ms, cvd), ...]."""
    out, acc = [], start
    for tr in trades:
        acc += tr.signed_size
        out.append((tr.ts, acc))
    return out


def delta_buckets(trades: Iterable[Trade], interval_ms: int) -> List[dict]:
    """Per-interval buy/sell volume: [{t, buy, sell, delta, n}] (t in seconds)."""
    buckets: List[dict] = []
    cur: Optional[dict] = None
    for tr in trades:
        b0 = bucket_start(tr.ts, interval_ms) // 1000
        if cur is None or cur["t"] != b0:
            cur = {"t": b0, "buy": 0.0, "sell": 0.0, "delta": 0.0, "n": 0}
            buckets.append(cur)
        if tr.side == "buy":
            cur["buy"] += tr.size
        else:
            cur["sell"] += tr.size
        cur["delta"] = cur["buy"] - cur["sell"]
        cur["n"] += 1
    return buckets


def cvd_from_buckets(buckets: Iterable[dict], start: float = 0.0) -> List[Tuple[int, float]]:
    out, acc = [], start
    for b in buckets:
        acc += b["delta"]
        out.append((b["t"], acc))
    return out


def approx_bar_delta(bar: Bar) -> float:
    """Close-location estimate: +v when close is at the high, -v at the low."""
    rng = bar.h - bar.l
    if rng <= 0 or bar.v <= 0:
        return 0.0
    return bar.v * ((bar.c - bar.l) - (bar.h - bar.c)) / rng


def approx_cvd_from_bars(bars: Iterable[Bar], start: float = 0.0) -> List[Tuple[int, float]]:
    out, acc = [], start
    for b in bars:
        acc += approx_bar_delta(b)
        out.append((b.t, acc))
    return out


def divergence(prices: Sequence[float], cvds: Sequence[float], lookback: int = 20) -> Optional[str]:
    """Compare the latest point with the extreme of the previous ``lookback``.

    * price makes a higher high but CVD does not -> ``"bearish"``
    * price makes a lower low  but CVD does not -> ``"bullish"``
    Returns ``None`` when neither holds or there is not enough data.
    """
    n = min(len(prices), len(cvds))
    if n < lookback + 1:
        return None
    p_now, c_now = prices[n - 1], cvds[n - 1]
    p_prev, c_prev = prices[n - 1 - lookback:n - 1], cvds[n - 1 - lookback:n - 1]
    if p_now > max(p_prev) and c_now < max(c_prev):
        return "bearish"
    if p_now < min(p_prev) and c_now > min(c_prev):
        return "bullish"
    return None
