"""Volume profile by price level, POC, value area and HVN/LVN nodes.

``profile_from_trades`` is exact and keeps buy/sell split. ``profile_from_bars``
spreads each bar's volume evenly across its high-low range; it is marked
``approximate`` because that is all a candle can tell us.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

from feeds.types import Bar, Trade


def level_of(price: float, tick: float) -> float:
    """Snap a price to the nearest tick, rounded so floats compare cleanly."""
    digits = max(0, -_exp10(tick)) + 2
    return round(round(price / tick) * tick, digits)


def _exp10(x: float) -> int:
    e = 0
    while x < 1 and e > -12:
        x *= 10
        e -= 1
    return e


class Profile:
    def __init__(self, tick: float, approximate: bool = False):
        self.tick = tick
        self.approximate = approximate
        self.levels: Dict[float, Dict[str, float]] = {}

    def add(self, price: float, size: float, side: str | None) -> None:
        lv = self.levels.setdefault(level_of(price, self.tick), {"buy": 0.0, "sell": 0.0, "total": 0.0})
        if side == "buy":
            lv["buy"] += size
        elif side == "sell":
            lv["sell"] += size
        lv["total"] += size

    @property
    def total(self) -> float:
        return sum(v["total"] for v in self.levels.values())

    def sorted_levels(self) -> List[Tuple[float, Dict[str, float]]]:
        return sorted(self.levels.items())

    def rows(self) -> List[dict]:
        return [{"price": p, **v} for p, v in self.sorted_levels()]


def profile_from_trades(trades: Iterable[Trade], tick: float) -> Profile:
    prof = Profile(tick)
    for tr in trades:
        prof.add(tr.price, tr.size, tr.side)
    return prof


def profile_from_bars(bars: Iterable[Bar], tick: float) -> Profile:
    prof = Profile(tick, approximate=True)
    for b in bars:
        lo, hi = level_of(b.l, tick), level_of(b.h, tick)
        n = int(round((hi - lo) / tick)) + 1
        share = b.v / n
        for i in range(n):
            prof.add(lo + i * tick, share, None)
    return prof


def poc(prof: Profile) -> float | None:
    if not prof.levels:
        return None
    return max(prof.levels.items(), key=lambda kv: (kv[1]["total"], -kv[0]))[0]


def value_area(prof: Profile, pct: float = 0.70) -> Tuple[float, float] | None:
    """Smallest contiguous band around the POC holding ``pct`` of volume.
    Expands one level at a time toward whichever neighbour has more volume."""
    levels = prof.sorted_levels()
    if not levels:
        return None
    prices = [p for p, _ in levels]
    vols = [v["total"] for _, v in levels]
    i = vols.index(max(vols))
    lo = hi = i
    acc, target = vols[i], prof.total * pct
    while acc < target and (lo > 0 or hi < len(vols) - 1):
        down = vols[lo - 1] if lo > 0 else -1.0
        up = vols[hi + 1] if hi < len(vols) - 1 else -1.0
        if up > down:
            hi += 1
            acc += vols[hi]
        else:
            lo -= 1
            acc += vols[lo]
    return prices[lo], prices[hi]


def session_range(prof: Profile, pct: float = 0.70) -> dict | None:
    """High/low/POC/value-area summary of a finished session.
    Use ``pct=0.30`` for a tight "magnet" band, ``0.70`` for a classic value area."""
    if not prof.levels:
        return None
    levels = prof.sorted_levels()
    va = value_area(prof, pct)
    return {"high": levels[-1][0], "low": levels[0][0], "poc": poc(prof),
            "va_low": va[0], "va_high": va[1], "va_pct": pct,
            "total": prof.total, "approximate": prof.approximate}


def nodes(prof: Profile, top: int = 3) -> dict:
    """High- and low-volume nodes. LVNs include empty levels inside the range
    (price gaps are the fast lanes)."""
    levels = prof.sorted_levels()
    if not levels:
        return {"hvn": [], "lvn": []}
    lo, hi = levels[0][0], levels[-1][0]
    n = int(round((hi - lo) / prof.tick)) + 1
    full = [(level_of(lo + i * prof.tick, prof.tick), prof.levels.get(level_of(lo + i * prof.tick, prof.tick), {"total": 0.0})["total"]) for i in range(n)]
    hvn = sorted(full, key=lambda x: -x[1])[:top]
    lvn = sorted(full, key=lambda x: x[1])[:top]
    return {"hvn": [{"price": p, "total": v} for p, v in hvn],
            "lvn": [{"price": p, "total": v} for p, v in lvn]}
