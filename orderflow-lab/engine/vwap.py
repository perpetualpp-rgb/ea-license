"""Session VWAP with volume-weighted standard-deviation bands.

Anchored at the first bar given; pass a session's bars to get a session VWAP.
``band_shape`` tells whether the bands are widening fast (a move is expanding,
don't fade it) or flat (range, mean-reversion territory).
"""
from __future__ import annotations

import math
from typing import Iterable, List, Sequence

from feeds.types import Bar


def vwap_bands(bars: Iterable[Bar], sigmas: Sequence[float] = (1.0, 2.0, 3.0)) -> List[dict]:
    out: List[dict] = []
    s_v = s_pv = s_p2v = 0.0
    for b in bars:
        p, v = b.typical, b.v
        s_v += v
        s_pv += p * v
        s_p2v += p * p * v
        if s_v <= 0:
            continue
        vwap = s_pv / s_v
        var = max(0.0, s_p2v / s_v - vwap * vwap)
        sd = math.sqrt(var)
        out.append({"t": b.t, "vwap": vwap, "sd": sd,
                    "bands": {k: (vwap - k * sd, vwap + k * sd) for k in sigmas}})
    return out


def position(price: float, point: dict) -> dict:
    """Where ``price`` sits relative to a vwap point: z-score and a label."""
    sd = point["sd"]
    z = (price - point["vwap"]) / sd if sd > 0 else 0.0
    if z >= 2:
        label = "expensive"
    elif z <= -2:
        label = "cheap"
    elif z > 0:
        label = "above"
    elif z < 0:
        label = "below"
    else:
        label = "at_vwap"
    return {"z": z, "label": label}


def band_shape(series: Sequence[dict], lookback: int = 12, expand: float = 1.25, flat: float = 1.05) -> str | None:
    """Compare band width now vs ``lookback`` points ago."""
    if len(series) <= lookback:
        return None
    now, then = series[-1]["sd"], series[-1 - lookback]["sd"]
    if then <= 0:
        return None
    ratio = now / then
    if ratio >= expand:
        return "expanding"
    if ratio <= flat:
        return "flat"
    return "widening"
