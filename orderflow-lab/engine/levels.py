"""Round-number levels (e.g. every 25 for gold, every 500 for BTC)."""
from __future__ import annotations

import math
from typing import List, Tuple


def round_levels(low: float, high: float, step: float) -> List[float]:
    first = math.ceil(low / step) * step
    out = []
    x = first
    while x <= high + 1e-9:
        out.append(round(x, 8))
        x += step
    return out


def nearest(price: float, step: float) -> Tuple[float, float]:
    below = math.floor(price / step) * step
    above = below + step if below < price else below
    return round(below, 8), round(above, 8)
