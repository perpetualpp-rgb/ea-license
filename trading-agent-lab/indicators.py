"""Pure-Python technical indicators (no numpy/pandas dependency).

All functions take a list of float prices ordered oldest -> newest and return
either the latest value or a full aligned series, as documented per function.
"""
from __future__ import annotations

from typing import List, Optional


def sma(values: List[float], period: int) -> Optional[float]:
    """Simple moving average of the last `period` values."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(values: List[float], period: int) -> List[float]:
    """Exponential moving average as a series aligned 1:1 with `values`."""
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def ema(values: List[float], period: int) -> Optional[float]:
    """Latest EMA value."""
    if len(values) < period:
        return None
    return ema_series(values, period)[-1]


def rsi(values: List[float], period: int = 14) -> Optional[float]:
    """Wilder's RSI of the most recent `period` deltas."""
    if len(values) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(values: List[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """Return (macd_line, signal_line, histogram) latest values, or Nones."""
    if len(values) < slow:
        return None, None, None
    fast_s = ema_series(values, fast)
    slow_s = ema_series(values, slow)
    macd_line = [f - s for f, s in zip(fast_s, slow_s)]
    signal_s = ema_series(macd_line, signal)
    line = macd_line[-1]
    sig = signal_s[-1]
    return line, sig, line - sig


def bollinger(values: List[float], period: int = 20, mult: float = 2.0):
    """Return (upper, middle, lower) Bollinger Bands latest values, or Nones."""
    if len(values) < period:
        return None, None, None
    window = values[-period:]
    mid = sum(window) / period
    variance = sum((x - mid) ** 2 for x in window) / period
    std = variance ** 0.5
    return mid + mult * std, mid, mid - mult * std
