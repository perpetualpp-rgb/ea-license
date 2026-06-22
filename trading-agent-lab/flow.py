#!/usr/bin/env python3
"""Flows Agent - a declarative, node-based algo-trading pipeline (PAPER ONLY).

Where ``trading_analyzer.py`` hard-codes one orchestration, the Flows Agent runs
whatever node sequence you declare in ``flow.json`` - the "Flows Agent" idea: a
trading strategy expressed as a wired graph of small, observable steps you can
reorder, add to, or drop without touching code.

The default flow:

    market_data -> strategy_agents -> fortress -> consensus -> risk -> execute -> log

i.e. pull data, gather the 3 strategy agents' votes, add the Fortress
multi-factor confirmation vote, take the N-of-M consensus, size it with
regime-aware fractional Kelly, paper-execute, and log. Each node writes into a
shared context and appends to a run trace, so every cycle is fully auditable
(see vault/flow-runs/).

Cron (one cycle per tick):
    * * * * * /usr/bin/python3 /path/to/flow.py --once

SAFETY: never connects to an exchange, never places a real order. ``--live`` and
``live_trading: true`` are refused, exactly as in trading_analyzer.py.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict

from flow_nodes import build_node
from llm import LLMClient

HERE = os.path.dirname(os.path.abspath(__file__))


def load_json(path: str) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state(path: str) -> Dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"prices": None, "prod": None, "losing_streak": 0}


def save_state(path: str, state: Dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


class Flow:
    """Builds nodes from a flow spec and runs them over a shared context."""

    def __init__(self, spec: Dict, cfg: Dict, vault: str, llm=None):
        self.name = spec.get("name", "flow")
        self.nodes = [build_node(n) for n in spec["nodes"]]
        self.cfg = cfg
        self.vault = vault
        self.llm = llm

    def run(self, state: Dict) -> Dict:
        ctx: Dict = {"cfg": self.cfg, "state": state, "vault": self.vault,
                     "llm": self.llm, "trace": []}
        for node in self.nodes:
            node.run(ctx)
        return ctx


def print_run(flow_name: str, ctx: Dict) -> None:
    snap = ctx["snapshot"]
    risk = ctx.get("risk", {})
    print(f"[{snap['time']}] flow={flow_name} {snap['source']} "
          f"price={snap['price']:.2f} -> {ctx.get('decision')} "
          f"({risk.get('regime')}, size {risk.get('fraction', 0):.3f})")
    for i, t in enumerate(ctx["trace"], 1):
        print(f"    {i}. {t['node']:<16} {t['summary']}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Flows Agent (declarative paper-trading pipeline)")
    p.add_argument("--flow", default=os.path.join(HERE, "flow.json"))
    p.add_argument("--config", default=os.path.join(HERE, "config.json"))
    p.add_argument("--vault", default=None)
    p.add_argument("--symbol", default=None, help="symbol to trade, e.g. BTCUSDT or XAUUSD")
    p.add_argument("--source", default=None, choices=["simulated", "tradingview"])
    p.add_argument("--once", action="store_true", help="run exactly one cycle (for cron)")
    p.add_argument("--cycles", type=int, default=0, help="number of cycles (0 = infinite)")
    p.add_argument("--interval", type=int, default=180, help="seconds between cycles")
    p.add_argument("--no-llm", action="store_true", help="force the indicator rules")
    p.add_argument("--model", default=None, help="override the LLM model id")
    p.add_argument("--live", action="store_true", help="(refused) live trading is not supported")
    args = p.parse_args(argv)

    if args.live:
        print("ERROR: live trading is intentionally NOT implemented. The Flows "
              "Agent is paper-trading only. See README.md.", file=sys.stderr)
        return 2

    cfg = load_json(args.config)
    if args.source:
        cfg["source"] = args.source
    if args.symbol:
        cfg["symbol"] = args.symbol
    if cfg.get("live_trading"):
        print("ERROR: config has live_trading=true but live trading is not "
              "supported. Set it to false.", file=sys.stderr)
        return 2

    llm_cfg = dict(cfg.get("llm", {}))
    if args.no_llm:
        llm_cfg["enabled"] = False
    if args.model:
        llm_cfg["model"] = args.model
    llm = LLMClient(llm_cfg)
    print(f"# {llm.status()}", file=sys.stderr)

    spec = load_json(args.flow)
    vault = args.vault or os.path.join(HERE, cfg.get("vault", "vault"))
    state_path = os.path.join(vault, f"state_{cfg['symbol']}.json")
    flow = Flow(spec, cfg, vault, llm)
    print(f"# flow '{flow.name}': {' -> '.join(n.type for n in flow.nodes)}",
          file=sys.stderr)

    def one():
        state = load_state(state_path)
        ctx = flow.run(state)
        save_state(state_path, state)
        print_run(flow.name, ctx)

    if args.once:
        one()
        return 0

    count = 0
    try:
        while True:
            one()
            count += 1
            if args.cycles and count >= args.cycles:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
