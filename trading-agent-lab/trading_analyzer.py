#!/usr/bin/env python3
"""Trading Agent Lab - main orchestrator (PAPER TRADING ONLY).

Implements the workflow from the Setup Doc:

  lab  : the 3 agents each paper-trade their own budget every cycle; we log
         per-agent P/L and crown the current winning agent.
  prod : take the >=2-of-3 consensus signal and paper-trade one production
         portfolio, verifying the signal against the (simulated) TradingView
         MCP snapshot before acting.

Cron usage (run one cycle per tick, as in the Doc):
  * * * * * /usr/bin/python3 /path/to/trading_analyzer.py --mode prod --once

Or run a self-looping session for local experiments:
  python3 trading_analyzer.py --mode lab --interval 180 --cycles 5

SAFETY: this program never connects to an exchange and never places a real
order. --live is intentionally refused (see paper_broker.py / README).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, List

import market_data
from agents import build_agents
from decision import consensus
from obsidian_logger import write_dashboards, write_session_log
from paper_broker import PaperPortfolio

HERE = os.path.dirname(os.path.abspath(__file__))


def load_config(path: str) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state(path: str) -> Dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"prices": None, "portfolios": {}, "prod": None}


def save_state(path: str, state: Dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def run_cycle(cfg: Dict, state: Dict, vault: str, mode: str) -> Dict:
    """Run a single trading cycle and persist Obsidian notes. Returns a summary."""
    profile = cfg.get("markets", {}).get(cfg["symbol"])
    snap = market_data.get_snapshot(
        cfg["symbol"], cfg["timeframe"], state, cfg.get("source", "simulated"), profile
    )
    price = snap["price"]
    agents = build_agents(cfg["agents"])
    portfolios = state.setdefault("portfolios", {})

    rows: List[Dict] = []
    signals = []
    for ag in agents:
        sig = ag.decide(snap)
        signals.append(sig)
        pf = PaperPortfolio(ag.budget, portfolios.get(ag.id))
        # In lab mode every agent acts on its own conviction.
        order = pf.execute(sig.action, price) if mode == "lab" else "no-op (prod)"
        portfolios[ag.id] = pf.to_state()
        rows.append({
            "id": ag.id, "name": ag.name, "action": sig.action, "reason": sig.reason,
            "order": order, "pnl": pf.pnl(price), "equity": pf.equity(price),
            "budget": ag.budget,
        })

    decision, votes = consensus(signals, cfg.get("decision", {}).get("consensus_required", 2))

    # Production portfolio trades only on consensus.
    if mode == "prod":
        prod_budget = sum(a["budget"] for a in cfg["agents"])
        prod_pf = PaperPortfolio(prod_budget, state.get("prod"))
        prod_order = prod_pf.execute(decision, price)
        state["prod"] = prod_pf.to_state()
        rows.append({
            "id": "PROD", "name": "Consensus Portfolio", "action": decision,
            "reason": f"{votes['BUY']}xBUY / {votes['SELL']}xSELL / {votes['HOLD']}xHOLD",
            "order": prod_order, "pnl": prod_pf.pnl(price), "equity": prod_pf.equity(price),
            "budget": prod_budget,
        })

    winner = max(rows, key=lambda r: r["pnl"])["id"]
    # "Stable" when the agents reach a quorum, "Unstable" when they disagree.
    status = "Stable" if max(votes.values()) >= 2 else "Unstable"

    log_path = write_session_log(vault, snap, rows, winner, decision, votes, status)
    write_dashboards(vault, snap, rows, winner, decision, status)

    return {
        "time": snap["time"], "price": price, "decision": decision, "votes": votes,
        "winner": winner, "rows": rows, "log_path": log_path, "source": snap["source"],
    }


def print_summary(s: Dict) -> None:
    print(f"[{s['time']}] {s['source']} | price={s['price']:.2f} | "
          f"decision={s['decision']} {s['votes']} | winner={s['winner']}")
    for r in s["rows"]:
        print(f"    {r['id']:<6} {r['action']:<4} P/L {r['pnl']:+10.2f}  {r['order']}  | {r['reason']}")
    print(f"    -> log: {s['log_path']}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Trading Agent Lab (paper trading)")
    p.add_argument("--mode", choices=["lab", "prod"], default="lab")
    p.add_argument("--interval", type=int, default=180, help="seconds between cycles when looping")
    p.add_argument("--cycles", type=int, default=0, help="number of cycles (0 = infinite loop)")
    p.add_argument("--once", action="store_true", help="run exactly one cycle (for cron)")
    p.add_argument("--config", default=os.path.join(HERE, "config.json"))
    p.add_argument("--vault", default=None, help="override vault dir from config")
    p.add_argument("--source", default=None, choices=["simulated", "tradingview"])
    p.add_argument("--symbol", default=None, help="symbol to trade, e.g. BTCUSDT or XAUUSD")
    p.add_argument("--live", action="store_true", help="(refused) live trading is not supported")
    args = p.parse_args(argv)

    if args.live:
        print("ERROR: live trading is intentionally NOT implemented. This lab is "
              "paper-trading only. See README.md to wire your own broker at your "
              "own risk.", file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    if args.source:
        cfg["source"] = args.source
    if args.symbol:
        cfg["symbol"] = args.symbol
    if cfg.get("live_trading"):
        print("ERROR: config has live_trading=true but live trading is not "
              "supported. Set it to false.", file=sys.stderr)
        return 2

    vault = args.vault or os.path.join(HERE, cfg.get("vault", "vault"))
    # Isolate state per symbol so BTCUSDT and XAUUSD keep separate price
    # histories and portfolios.
    state_path = os.path.join(vault, f"state_{cfg['symbol']}.json")

    def one():
        state = load_state(state_path)
        summary = run_cycle(cfg, state, vault, args.mode)
        save_state(state_path, state)
        print_summary(summary)

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
