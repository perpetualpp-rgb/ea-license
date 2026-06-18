"""The three competing trading agents from the Lab spec.

Each agent reads the same market snapshot but interprets it through its own
strategy lens and emits a Signal (BUY / SELL / HOLD) with a human-readable
reason. Agents are pure decision functions - position/cash accounting lives in
paper_broker.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

BUY = "BUY"
SELL = "SELL"
HOLD = "HOLD"


@dataclass
class Signal:
    action: str
    reason: str


class Agent:
    strategy = "base"

    def __init__(self, agent_id: str, name: str, budget: float):
        self.id = agent_id
        self.name = name
        self.budget = budget

    def decide(self, snap: Dict) -> Signal:  # pragma: no cover - overridden
        raise NotImplementedError


class TrendFollowingAgent(Agent):
    """Long when the fast EMA leads the slow EMA and MACD confirms momentum."""

    strategy = "trend_following"

    def decide(self, snap: Dict) -> Signal:
        ema_fast, ema_slow = snap.get("ema_fast"), snap.get("ema_slow")
        macd, sig = snap.get("macd"), snap.get("macd_signal")
        if None in (ema_fast, ema_slow, macd, sig):
            return Signal(HOLD, "insufficient data")
        if ema_fast > ema_slow and macd > sig:
            return Signal(BUY, f"uptrend: EMA12>{ema_slow:.0f} & MACD>signal")
        if ema_fast < ema_slow and macd < sig:
            return Signal(SELL, "downtrend: EMA12<EMA26 & MACD<signal")
        return Signal(HOLD, "no clear trend")


class MeanReversionAgent(Agent):
    """Fade extremes: buy oversold (lower band / low RSI), sell overbought."""

    strategy = "mean_reversion"

    def decide(self, snap: Dict) -> Signal:
        price, rsi = snap.get("price"), snap.get("rsi")
        bb_upper, bb_lower = snap.get("bb_upper"), snap.get("bb_lower")
        if None in (price, rsi, bb_upper, bb_lower):
            return Signal(HOLD, "insufficient data")
        if price <= bb_lower or rsi < 30:
            return Signal(BUY, f"oversold: RSI={rsi:.0f}, price<=lower band")
        if price >= bb_upper or rsi > 70:
            return Signal(SELL, f"overbought: RSI={rsi:.0f}, price>=upper band")
        return Signal(HOLD, f"in range: RSI={rsi:.0f}")


class ScalpingAgent(Agent):
    """Short-horizon momentum: ride price above SMA with neutral-to-rising RSI."""

    strategy = "scalping"

    def decide(self, snap: Dict) -> Signal:
        price, sma20, rsi = snap.get("price"), snap.get("sma20"), snap.get("rsi")
        macd_hist = snap.get("macd_hist")
        if None in (price, sma20, rsi, macd_hist):
            return Signal(HOLD, "insufficient data")
        if price > sma20 and 45 <= rsi <= 65 and macd_hist > 0:
            return Signal(BUY, f"momentum up: price>SMA20, RSI={rsi:.0f}")
        if price < sma20 and macd_hist < 0:
            return Signal(SELL, "momentum down: price<SMA20, hist<0")
        return Signal(HOLD, "no scalp setup")


_REGISTRY = {
    "trend_following": TrendFollowingAgent,
    "mean_reversion": MeanReversionAgent,
    "scalping": ScalpingAgent,
}


def build_agents(configs):
    """Instantiate agents from the list of dicts in config.json."""
    agents = []
    for c in configs:
        cls = _REGISTRY.get(c["strategy"])
        if cls is None:
            raise ValueError(f"unknown strategy: {c['strategy']}")
        agents.append(cls(c["id"], c["name"], float(c["budget"])))
    return agents
