"""The "Fortress" multi-factor signal, adapted from `skill-algotrader`.

The skill (https://github.com/javajack/skill-algotrader) pitches a multi-factor
*confirmation* signal it calls **Fortress**: only fire when several independent
factors line up, rather than on any single indicator. Its production recipe is
RSI + ADX + volume. This lab's feed is close-only, so two of those become
honest proxies:

* RSI            -> taken straight from the snapshot.
* ADX            -> ``trend_strength`` (EMA-spread in ATR units; see indicators).
* volume thrust  -> price momentum vs SMA20 (we have no volume series).

The point the skill makes is the *gate*, not the exact inputs: require trend
strength **and** an RSI in the entry zone **and** momentum agreement before
taking a side. That is what gives it the higher hit-rate / fewer-but-cleaner
entries it advertises. It returns a normal lab ``Signal`` so it can vote in the
same consensus as the three strategy agents.
"""
from __future__ import annotations

from typing import Dict

from agents import BUY, HOLD, SELL, Signal


def fortress_signal(snap: Dict, cfg: Dict | None = None) -> Signal:
    """Multi-factor confirmation entry. HOLD unless every factor agrees.

    ``cfg`` is the optional ``fortress`` block in config.json:
        adx_min        min trend strength to act     (default 20)
        rsi_long_min   RSI floor for a long          (default 45)
        rsi_long_max   RSI ceiling for a long        (default 70)
        rsi_short_min  RSI floor for a short         (default 30)
        rsi_short_max  RSI ceiling for a short       (default 55)
    """
    c = cfg or {}
    adx_min = float(c.get("adx_min", 20.0))
    rsi_long_min = float(c.get("rsi_long_min", 45.0))
    rsi_long_max = float(c.get("rsi_long_max", 70.0))
    rsi_short_min = float(c.get("rsi_short_min", 30.0))
    rsi_short_max = float(c.get("rsi_short_max", 55.0))

    rsi = snap.get("rsi")
    ts = snap.get("trend_strength")
    price = snap.get("price")
    sma20 = snap.get("sma20")
    macd_hist = snap.get("macd_hist")
    if None in (rsi, ts, price, sma20, macd_hist):
        return Signal(HOLD, "[Fortress] insufficient data", engine="rule")

    # Factor 1: the tape must actually be trending (ADX proxy gate).
    if ts < adx_min:
        return Signal(HOLD, f"[Fortress] no trend (strength {ts:.0f} < {adx_min:.0f})",
                      engine="rule")

    # Factor 2 + 3: RSI in zone AND momentum (price vs SMA + MACD hist) agree.
    long_ok = (rsi_long_min <= rsi <= rsi_long_max) and price > sma20 and macd_hist > 0
    short_ok = (rsi_short_min <= rsi <= rsi_short_max) and price < sma20 and macd_hist < 0

    if long_ok:
        return Signal(BUY, f"[Fortress] trend {ts:.0f} + RSI {rsi:.0f} + up-momentum",
                      engine="rule")
    if short_ok:
        return Signal(SELL, f"[Fortress] trend {ts:.0f} + RSI {rsi:.0f} + down-momentum",
                      engine="rule")
    return Signal(HOLD, f"[Fortress] factors disagree (RSI {rsi:.0f}, trend {ts:.0f})",
                  engine="rule")
