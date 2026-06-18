"""The three competing trading agents from the Lab spec.

Each agent reads the same market snapshot but interprets it through its own
strategy lens and emits a Signal (BUY / SELL / HOLD) with a human-readable
reason. Agents are pure decision functions - position/cash accounting lives in
paper_broker.py.

Each agent can decide in one of two ways:

* **AI (Hermes / any OpenAI-compatible LLM)** - when an ``LLMClient`` is
  available, the agent sends its strategy persona + the indicator snapshot to
  the model and parses back a JSON {action, reason}. This is the "AI agent"
  behaviour from the Setup Doc.
* **Rule-based** - the deterministic indicator logic, used as a transparent
  fallback whenever the LLM is disabled, has no API key, or errors out.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, Optional

BUY = "BUY"
SELL = "SELL"
HOLD = "HOLD"
_VALID = {BUY, SELL, HOLD}


@dataclass
class Signal:
    action: str
    reason: str
    engine: str = "rule"  # "ai" when the decision came from the LLM


def _fmt(v) -> str:
    """Format a possibly-None indicator value for the prompt."""
    return "n/a" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))


def _snapshot_prompt(snap: Dict) -> str:
    """Render the indicator snapshot as a compact, model-friendly block."""
    return (
        f"Symbol: {snap.get('symbol')}  Timeframe: {snap.get('timeframe')}  "
        f"(data source: {snap.get('source')})\n"
        f"Price:  {_fmt(snap.get('price'))}\n"
        f"SMA20:  {_fmt(snap.get('sma20'))}\n"
        f"EMA12:  {_fmt(snap.get('ema_fast'))}   EMA26: {_fmt(snap.get('ema_slow'))}\n"
        f"RSI14:  {_fmt(snap.get('rsi'))}\n"
        f"MACD:   {_fmt(snap.get('macd'))}   signal: {_fmt(snap.get('macd_signal'))}   "
        f"hist: {_fmt(snap.get('macd_hist'))}\n"
        f"Bollinger: lower {_fmt(snap.get('bb_lower'))} / mid {_fmt(snap.get('bb_mid'))} "
        f"/ upper {_fmt(snap.get('bb_upper'))}"
    )


def _parse_signal(text: str) -> Optional[Signal]:
    """Extract a {action, reason} JSON object from the model's reply."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    action = str(data.get("action", "")).strip().upper()
    if action not in _VALID:
        return None
    reason = str(data.get("reason", "")).strip()[:160] or "ai decision"
    return Signal(action, f"[AI] {reason}", engine="ai")


class Agent:
    strategy = "base"
    # One-line strategy persona injected into the LLM system prompt.
    persona = "a disciplined trading agent"

    def __init__(self, agent_id: str, name: str, budget: float):
        self.id = agent_id
        self.name = name
        self.budget = budget

    def decide(self, snap: Dict, llm=None) -> Signal:
        """Decide via the LLM when available, else via the indicator rules."""
        if llm is not None and getattr(llm, "available", False):
            sig = self._decide_ai(snap, llm)
            if sig is not None:
                return sig
        return self._decide_rule(snap)

    def _system_prompt(self) -> str:
        return (
            f"You are {self.name}, an autonomous trading agent in a PAPER-TRADING lab.\n"
            f"Your strategy: {self.persona}\n"
            "Decide strictly according to YOUR strategy for the given snapshot. "
            "Do not invent data. Reply with ONLY one compact JSON object and no other "
            'text:\n{"action": "BUY" | "SELL" | "HOLD", "reason": "<=120 chars"}'
        )

    def _decide_ai(self, snap: Dict, llm) -> Optional[Signal]:
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": _snapshot_prompt(snap)},
        ]
        return _parse_signal(llm.chat(messages))

    def _decide_rule(self, snap: Dict) -> Signal:  # pragma: no cover - overridden
        raise NotImplementedError


class TrendFollowingAgent(Agent):
    """Long when the fast EMA leads the slow EMA and MACD confirms momentum."""

    strategy = "trend_following"
    persona = (
        "Trend Following — go long (BUY) when the fast EMA(12) leads the slow "
        "EMA(26) and MACD is above its signal line; go short (SELL) on the "
        "opposite; otherwise HOLD."
    )

    def _decide_rule(self, snap: Dict) -> Signal:
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
    persona = (
        "Mean Reversion — fade extremes: BUY when oversold (price at/below the "
        "lower Bollinger band or RSI < 30); SELL when overbought (price at/above "
        "the upper band or RSI > 70); HOLD while in range."
    )

    def _decide_rule(self, snap: Dict) -> Signal:
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
    persona = (
        "AI-based Scalping — short-horizon momentum: BUY when price is above "
        "SMA20 with neutral-to-rising RSI (about 45-65) and a positive MACD "
        "histogram; SELL on downward momentum (price below SMA20, negative "
        "histogram); otherwise HOLD."
    )

    def _decide_rule(self, snap: Dict) -> Signal:
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
