"""Risk & position sizing for the Flows Agent.

Ports the risk-management ideas from the `skill-algotrader` skill
(https://github.com/javajack/skill-algotrader) into this paper lab:

* **Market-regime detection** - classify the tape as ``trend`` / ``range`` /
  ``volatile`` from the trend-strength + volatility proxies, then size
  accordingly (trade trends bigger, fade chop smaller, stand down when wild).
* **Kelly Criterion sizing** - convert a configured edge (win rate + payoff)
  into a position fraction, then apply a fractional-Kelly haircut and a hard
  cap. Throttle on a losing streak (consecutive-loss brake).

Everything here only produces a *fraction of available cash* for the paper
broker to deploy. It places no real orders.
"""
from __future__ import annotations

from typing import Dict, Optional


def detect_regime(snap: Dict) -> str:
    """Classify the market into trend / range / volatile from the snapshot.

    Uses the close-only proxies (``trend_strength`` and ``atr`` relative to
    price). Mirrors the skill's "detect regime before sizing" rule.
    """
    ts = snap.get("trend_strength")
    atr = snap.get("atr")
    price = snap.get("price") or 0.0
    atr_pct = (atr / price * 100.0) if (atr and price) else 0.0

    if atr_pct >= 3.0:
        return "volatile"
    if ts is not None and ts >= 20.0:
        return "trend"
    return "range"


# How aggressively to size in each regime (multiplier on the Kelly fraction).
REGIME_SIZING = {"trend": 1.0, "range": 0.5, "volatile": 0.25}


def kelly_fraction(win_rate: float, payoff: float) -> float:
    """Full-Kelly bet fraction for a binary edge.

    f* = W - (1 - W) / R, where W = win rate and R = win/loss payoff ratio.
    Returns 0 when the edge is non-positive (never size into a losing bet).
    """
    if payoff <= 0:
        return 0.0
    f = win_rate - (1.0 - win_rate) / payoff
    return max(0.0, f)


def size_position(snap: Dict, cfg: Dict, losing_streak: int = 0) -> Dict:
    """Return a sizing decision: fraction of cash to deploy + the reasoning.

    ``cfg`` is the ``risk`` block from config.json (all optional):
        win_rate          assumed strategy edge          (default 0.55)
        payoff            avg win / avg loss             (default 1.5)
        kelly_fraction    fractional-Kelly haircut        (default 0.5)
        max_position      hard cap on fraction            (default 0.25)
        loss_throttle     halving per consecutive loss    (default 0.5)
        max_losing_streak stand-down after N losses        (default 4)
    """
    win_rate = float(cfg.get("win_rate", 0.55))
    payoff = float(cfg.get("payoff", 1.5))
    kelly_haircut = float(cfg.get("kelly_fraction", 0.5))
    max_position = float(cfg.get("max_position", 0.25))
    loss_throttle = float(cfg.get("loss_throttle", 0.5))
    max_streak = int(cfg.get("max_losing_streak", 4))

    regime = detect_regime(snap)

    # Consecutive-loss brake: stand fully down once the streak is too long.
    if losing_streak >= max_streak:
        return {
            "fraction": 0.0,
            "regime": regime,
            "reason": f"stand-down: {losing_streak} consecutive losses >= {max_streak}",
        }

    base = kelly_fraction(win_rate, payoff) * kelly_haircut
    regime_mult = REGIME_SIZING.get(regime, 0.5)
    throttle = loss_throttle ** losing_streak  # 1.0, 0.5, 0.25, ...
    fraction = min(max_position, base * regime_mult * throttle)

    reason = (
        f"{regime} regime x{regime_mult:g} | Kelly {base:.3f} "
        f"(W={win_rate:.0%}, R={payoff:g}, {kelly_haircut:g}x)"
    )
    if losing_streak:
        reason += f" | throttle x{throttle:g} ({losing_streak} losses)"
    return {"fraction": round(fraction, 4), "regime": regime, "reason": reason}
