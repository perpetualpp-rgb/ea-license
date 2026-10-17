"""Big trades: outlier-sized prints and where they cluster.

Threshold is a percentile of recent trade sizes (unit-agnostic, so it works
for 0.1-BTC prints and 20-contract prints alike) with an optional absolute
floor. A level where big buyers and big sellers both printed is ``contested``:
someone absorbed the other side there, and the level matters either way.
"""
from __future__ import annotations

import math
from typing import Iterable, List, Sequence

from feeds.types import Trade

from .profile import level_of


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile (pct in 0..1). Empty input -> 0."""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(math.ceil(pct * len(s))) - 1))
    return s[k]


def threshold(trades: Sequence[Trade], pct: float = 0.99, min_abs: float = 0.0) -> float:
    return max(min_abs, percentile([t.size for t in trades], pct))


def big_trades(trades: Iterable[Trade], thr: float) -> List[Trade]:
    return [t for t in trades if thr > 0 and t.size >= thr]


def zones(big: Iterable[Trade], tick: float, contest_ratio: float = 0.5) -> List[dict]:
    """Group big prints by price level. ``contested`` when the smaller side is at
    least ``contest_ratio`` of the larger side."""
    acc: dict = {}
    for t in big:
        z = acc.setdefault(level_of(t.price, tick), {"price": level_of(t.price, tick), "buy": 0.0, "sell": 0.0, "n": 0,
                                                        "first_ts": t.ts, "last_ts": t.ts})
        z["buy" if t.side == "buy" else "sell"] += t.size
        z["n"] += 1
        z["first_ts"], z["last_ts"] = min(z["first_ts"], t.ts), max(z["last_ts"], t.ts)
    rows = []
    for z in acc.values():
        big_side, small_side = max(z["buy"], z["sell"]), min(z["buy"], z["sell"])
        rows.append({**z, "total": z["buy"] + z["sell"], "delta": z["buy"] - z["sell"],
                     "contested": big_side > 0 and small_side / big_side >= contest_ratio})
    rows.sort(key=lambda r: -r["total"])
    return rows


def recent(big: Iterable[Trade], now_ms: int, window_ms: int) -> List[Trade]:
    return [t for t in big if now_ms - t.ts <= window_ms]
