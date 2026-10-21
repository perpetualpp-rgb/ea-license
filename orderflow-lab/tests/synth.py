"""Deterministic synthetic market data for tests (the repo ships no third-party data)."""
import random

from feeds.types import Bar


def synthetic_bars(n: int = 1500, seed: int = 11, start: float = 4180.0, t0: int = 1_790_000_000) -> list:
    """1-minute random-walk candles with prices on a 0.1 grid and integer volume."""
    rng = random.Random(seed)
    bars, price = [], start
    for i in range(n):
        o = price
        c = round(max(1.0, o + rng.gauss(0, 0.9)), 1)
        h = round(max(o, c) + abs(rng.gauss(0, 0.5)), 1)
        l = round(min(o, c) - abs(rng.gauss(0, 0.5)), 1)
        bars.append(Bar(t0 + 60 * i, o, h, l, c, float(rng.randint(5, 400))))
        price = c
    return bars
