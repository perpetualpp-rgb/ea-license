"""Market data provider for the Trading Agent Lab.

Two sources are supported:

* ``simulated`` (default) - a deterministic-ish random walk seeded from the
  persisted price history. Lets the whole lab run offline with zero external
  dependencies, which is what makes it safe to demo.
* ``tradingview`` - best-effort fetch of real technical analysis via the
  optional ``tradingview-ta`` package. This mirrors what the TradingView MCP
  server exposes (https://github.com/atilaahmettaner/tradingview-mcp). If the
  package or network is unavailable it transparently falls back to simulation
  and flags it in the snapshot.
"""
from __future__ import annotations

import random
import time
from typing import Dict, List

import indicators

# How many synthetic candles to bootstrap so indicators are valid immediately.
BOOTSTRAP = 60
# Minimum history kept in state to keep indicator math correct without bloat.
MAX_HISTORY = 240


def _bootstrap_prices(symbol: str, start: float) -> List[float]:
    """Generate a plausible starting price history for a fresh lab run."""
    rng = random.Random(hash(symbol) & 0xFFFFFFFF)
    price = float(start)
    prices = [round(price, 2)]
    for _ in range(BOOTSTRAP - 1):
        price *= 1 + rng.gauss(0, 0.004)
        prices.append(round(price, 2))
    return prices


def _next_simulated_price(prices: List[float]) -> float:
    """Random-walk one step forward from the latest known price."""
    last = prices[-1]
    drift = random.gauss(0, 0.005)
    return round(max(last * (1 + drift), 0.01), 2)


def _try_tradingview(symbol: str, timeframe: str, screener: str, exchange: str):
    """Best-effort real price via tradingview-ta; return None on any failure."""
    try:
        from tradingview_ta import TA_Handler, Interval  # type: ignore
    except Exception:
        return None
    interval_map = {
        "1m": "INTERVAL_1_MINUTE",
        "3m": "INTERVAL_3_MINUTES",
        "5m": "INTERVAL_5_MINUTES",
        "15m": "INTERVAL_15_MINUTES",
        "1h": "INTERVAL_1_HOUR",
    }
    try:
        handler = TA_Handler(
            symbol=symbol,
            screener=screener,
            exchange=exchange,
            interval=getattr(Interval, interval_map.get(timeframe, "INTERVAL_3_MINUTES")),
        )
        analysis = handler.get_analysis()
        return float(analysis.indicators["close"])
    except Exception:
        return None


# Sensible fallbacks when a symbol has no explicit market profile in config.
DEFAULT_PROFILE = {"start": 100.0, "screener": "crypto", "exchange": "BINANCE"}


def get_snapshot(symbol: str, timeframe: str, state: Dict, source: str = "simulated",
                 profile: Dict | None = None) -> Dict:
    """Advance the price series by one step and return a full indicator snapshot.

    The price history lives in ``state['prices']`` so consecutive cron
    invocations continue the same series. ``profile`` carries the per-symbol
    starting price and TradingView screener/exchange.
    """
    profile = {**DEFAULT_PROFILE, **(profile or {})}
    prices: List[float] = state.get("prices") or _bootstrap_prices(symbol, profile["start"])

    used_source = source
    price = None
    if source == "tradingview":
        price = _try_tradingview(symbol, timeframe, profile["screener"], profile["exchange"])
        if price is None:
            used_source = "simulated (tradingview unavailable)"
    if price is None:
        price = _next_simulated_price(prices)

    prices.append(price)
    if len(prices) > MAX_HISTORY:
        prices = prices[-MAX_HISTORY:]
    state["prices"] = prices

    macd_line, macd_signal, macd_hist = indicators.macd(prices)
    bb_upper, bb_mid, bb_lower = indicators.bollinger(prices)

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "source": used_source,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "price": price,
        "sma20": indicators.sma(prices, 20),
        "ema_fast": indicators.ema(prices, 12),
        "ema_slow": indicators.ema(prices, 26),
        "rsi": indicators.rsi(prices, 14),
        "macd": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "bb_upper": bb_upper,
        "bb_mid": bb_mid,
        "bb_lower": bb_lower,
    }
