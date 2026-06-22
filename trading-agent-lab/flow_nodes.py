"""Nodes for the Flows Agent pipeline.

A *flow* is an ordered list of nodes wired together by a shared ``ctx`` dict
(the n8n / "Flows Agent" idea: each node reads what earlier nodes wrote and
appends its own output). Every node records a one-line entry in ``ctx['trace']``
so a whole run is observable end-to-end, like a flow execution log.

The node set wires the existing lab pieces (market data, the three strategy
agents, paper broker, Obsidian logger) together with the ported quant brain
(``fortress`` signal, ``risk`` sizing) into a single configurable pipeline.

NO REAL ORDERS: the execute node only ever touches the paper portfolio.
"""
from __future__ import annotations

from typing import Dict, List

import indicators
import market_data
from agents import BUY, HOLD, SELL, Signal, build_agents
from decision import consensus
from fortress import fortress_signal
from obsidian_logger import write_dashboards, write_session_log
from paper_broker import PaperPortfolio
from risk import size_position


class Node:
    """Base node. Subclasses implement ``run(ctx)`` and set ``type``."""

    type = "base"

    def __init__(self, name: str, params: Dict | None = None):
        self.name = name
        self.params = params or {}

    def run(self, ctx: Dict) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _trace(self, ctx: Dict, summary: str, **extra) -> None:
        ctx["trace"].append({"node": self.name, "type": self.type,
                             "summary": summary, **extra})


class MarketDataNode(Node):
    """Pull a snapshot and augment it with the ATR / trend-strength proxies."""

    type = "market_data"

    def run(self, ctx: Dict) -> None:
        cfg, state = ctx["cfg"], ctx["state"]
        profile = cfg.get("markets", {}).get(cfg["symbol"])
        source = self.params.get("source") or cfg.get("source", "simulated")
        snap = market_data.get_snapshot(cfg["symbol"], cfg["timeframe"], state,
                                        source, profile)
        prices = state.get("prices") or []
        snap["atr"] = indicators.atr(prices, 14)
        snap["trend_strength"] = indicators.trend_strength(prices)
        ctx["snapshot"] = snap
        self._trace(ctx, f"{snap['source']} | price={snap['price']:.2f} "
                         f"RSI={snap.get('rsi') or float('nan'):.0f} "
                         f"trend={snap.get('trend_strength') or float('nan'):.0f}",
                    price=snap["price"], source=snap["source"])


class StrategyAgentsNode(Node):
    """Run the three lab agents (LLM or rules) and add their votes."""

    type = "strategy_agents"

    def run(self, ctx: Dict) -> None:
        snap, llm = ctx["snapshot"], ctx.get("llm")
        votes: List = ctx.setdefault("votes_detail", [])
        signals: List[Signal] = ctx.setdefault("signals", [])
        for ag in build_agents(ctx["cfg"]["agents"]):
            sig = ag.decide(snap, llm)
            signals.append(sig)
            votes.append({"id": ag.id, "name": ag.name, "signal": sig,
                          "budget": ag.budget})
        ai = sum(1 for v in votes if v["signal"].engine == "ai")
        ctx["ai_agents"] = ai
        actions = " ".join(f"{v['id']}={v['signal'].action}" for v in votes)
        self._trace(ctx, f"{len(votes)} agents ({ai} via AI): {actions}")


class FortressNode(Node):
    """Add the multi-factor Fortress signal as an extra confirming voter."""

    type = "fortress"

    def run(self, ctx: Dict) -> None:
        sig = fortress_signal(ctx["snapshot"], ctx["cfg"].get("fortress"))
        ctx.setdefault("signals", []).append(sig)
        ctx.setdefault("votes_detail", []).append(
            {"id": "FORTRESS", "name": "Fortress", "signal": sig, "budget": 0.0})
        self._trace(ctx, f"{sig.action} :: {sig.reason}")


class ConsensusNode(Node):
    """Collapse all collected signals into one decision via N-of-M majority."""

    type = "consensus"

    def run(self, ctx: Dict) -> None:
        required = self.params.get(
            "consensus_required",
            ctx["cfg"].get("decision", {}).get("consensus_required", 2))
        decision, votes = consensus(ctx.get("signals", []), required)
        ctx["decision"] = decision
        ctx["vote_tally"] = votes
        ctx["status"] = "Stable" if max(votes.values()) >= required else "Unstable"
        self._trace(ctx, f"decision={decision} (>= {required} of "
                         f"{sum(votes.values())}) {votes}")


class RiskNode(Node):
    """Size the position with regime-aware fractional Kelly + loss throttle."""

    type = "risk"

    def run(self, ctx: Dict) -> None:
        streak = int(ctx["state"].get("losing_streak", 0))
        sizing = size_position(ctx["snapshot"], ctx["cfg"].get("risk", {}), streak)
        ctx["risk"] = sizing
        self._trace(ctx, f"regime={sizing['regime']} fraction={sizing['fraction']:.3f} "
                         f":: {sizing['reason']}")


class ExecuteNode(Node):
    """Apply the decision to the paper portfolio at the risk-sized fraction."""

    type = "execute"

    def run(self, ctx: Dict) -> None:
        cfg, state, snap = ctx["cfg"], ctx["state"], ctx["snapshot"]
        price, decision = snap["price"], ctx.get("decision", HOLD)
        fraction = ctx.get("risk", {}).get("fraction", 1.0)

        budget = sum(a["budget"] for a in cfg["agents"])
        pf = PaperPortfolio(budget, state.get("prod"))
        equity_before = pf.equity(price)
        order = pf.execute(decision, price, fraction if decision == BUY else 1.0)
        state["prod"] = pf.to_state()

        # Track a realized-PnL losing streak to drive the risk throttle.
        if decision == SELL and order != "no-op":
            if pf.equity(price) < equity_before:
                state["losing_streak"] = int(state.get("losing_streak", 0)) + 1
            else:
                state["losing_streak"] = 0

        ctx["order"] = order
        ctx["prod_pnl"] = pf.pnl(price)
        ctx["prod_equity"] = pf.equity(price)
        ctx["prod_budget"] = budget
        self._trace(ctx, f"{order} | equity={pf.equity(price):.2f} "
                         f"P/L={pf.pnl(price):+.2f}")


class ObsidianLogNode(Node):
    """Write the per-cycle vault notes + a flow-trace note for this run."""

    type = "obsidian_log"

    def run(self, ctx: Dict) -> None:
        vault, snap = ctx["vault"], ctx["snapshot"]
        rows = []
        for v in ctx.get("votes_detail", []):
            sig = v["signal"]
            rows.append({"id": v["id"], "name": v["name"], "action": sig.action,
                         "reason": sig.reason, "order": "vote", "pnl": 0.0,
                         "equity": v["budget"], "budget": v["budget"]})
        rows.append({"id": "PROD", "name": "Flows Portfolio",
                     "action": ctx.get("decision", HOLD),
                     "reason": ctx.get("risk", {}).get("reason", ""),
                     "order": ctx.get("order", "no-op"),
                     "pnl": ctx.get("prod_pnl", 0.0),
                     "equity": ctx.get("prod_equity", 0.0),
                     "budget": ctx.get("prod_budget", 0.0)})

        path = write_session_log(vault, snap, rows, "PROD",
                                 ctx.get("decision", HOLD), ctx.get("vote_tally", {}),
                                 ctx.get("status", "Unstable"),
                                 ai_agents=ctx.get("ai_agents", 0))
        write_dashboards(vault, snap, rows, "PROD", ctx.get("decision", HOLD),
                         ctx.get("status", "Unstable"))
        _write_flow_trace(vault, ctx)
        ctx["log_path"] = path
        self._trace(ctx, f"wrote {path}")


def _write_flow_trace(vault: str, ctx: Dict) -> None:
    """Persist the node-by-node execution trace as its own vault note."""
    import os
    snap = ctx["snapshot"]
    fname = "Flow_" + snap["time"].replace(":", "-").replace(" ", "_") + ".md"
    os.makedirs(os.path.join(vault, "flow-runs"), exist_ok=True)
    path = os.path.join(vault, "flow-runs", fname)
    lines = [f"### \U0001F501 Flow run: {snap['time']}", "",
             f"- Symbol: {snap['symbol']} @ {snap['timeframe']} ({snap['source']})", "",
             "| # | Node | Type | Output |", "|---|------|------|--------|"]
    for i, t in enumerate(ctx["trace"], 1):
        # Escape pipes so a summary like "a | b" doesn't split the table column.
        summary = t["summary"].replace("|", "\\|")
        lines.append(f"| {i} | {t['node']} | {t['type']} | {summary} |")
    lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# Map flow.json "type" strings to node classes.
REGISTRY = {
    "market_data": MarketDataNode,
    "strategy_agents": StrategyAgentsNode,
    "fortress": FortressNode,
    "consensus": ConsensusNode,
    "risk": RiskNode,
    "execute": ExecuteNode,
    "obsidian_log": ObsidianLogNode,
}


def build_node(spec: Dict) -> Node:
    """Instantiate a node from a flow.json step ``{type, name?, params?}``."""
    cls = REGISTRY.get(spec["type"])
    if cls is None:
        raise ValueError(f"unknown node type: {spec['type']} "
                         f"(known: {', '.join(sorted(REGISTRY))})")
    return cls(spec.get("name", spec["type"]), spec.get("params"))
