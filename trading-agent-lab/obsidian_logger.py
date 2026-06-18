"""Writes Markdown notes into an Obsidian-style vault.

Reproduces the templates from the Setup Doc:
  * trading-logs/Session_*.md  - one note per cycle (the Log Template)
  * Daily_Market_Summary.md    - rolling market sentiment line
  * wallet_balance.md          - current paper balances (Risk Check)
  * last_trade_log.md          - heartbeat for Cronjob Health
"""
from __future__ import annotations

import os
from typing import Dict, List


def _ensure(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_session_log(vault: str, snap: Dict, rows: List[Dict], winner: str,
                      decision: str, votes: Dict, status: str) -> str:
    """Append/create the per-cycle session note. Returns the file path."""
    logs_dir = os.path.join(vault, "trading-logs")
    _ensure(logs_dir)
    fname = "Session_" + snap["time"].replace(":", "-").replace(" ", "_") + ".md"
    path = os.path.join(logs_dir, fname)

    perf = " | ".join(f"{r['id']} (P/L {r['pnl']:+.2f})" for r in rows)
    lines = [
        f"### \U0001F4C8 Trading Session: {snap['time']}",
        "",
        f"- **Symbol:** {snap['symbol']} @ {snap['timeframe']}  (source: {snap['source']})",
        f"- **Price:** {snap['price']:.2f}",
        f"- **Performance:** {perf}",
        f"- **Winning Agent:** {winner}",
        f"- **Consensus Decision:** {decision}  (votes BUY={votes['BUY']} "
        f"SELL={votes['SELL']} HOLD={votes['HOLD']})",
        f"- **Status:** {status}",
        "",
        "| Agent | Signal | Reason | Order | P/L |",
        "|-------|--------|--------|-------|-----|",
    ]
    for r in rows:
        lines.append(
            f"| {r['id']} {r['name']} | {r['action']} | {r['reason']} "
            f"| {r['order']} | {r['pnl']:+.2f} |"
        )
    lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def write_dashboards(vault: str, snap: Dict, rows: List[Dict], winner: str,
                     decision: str, status: str) -> None:
    """Update the always-current checklist notes referenced by the Doc."""
    _ensure(vault)

    sentiment = "Bullish" if decision == "BUY" else "Bearish" if decision == "SELL" else "Neutral"
    rsi_line = f"- RSI: {snap['rsi']:.1f}\n" if snap.get("rsi") is not None else ""
    with open(os.path.join(vault, "Daily_Market_Summary.md"), "w", encoding="utf-8") as f:
        f.write(
            f"# Daily Market Summary\n\n"
            f"- Updated: {snap['time']}\n"
            f"- {snap['symbol']} price: {snap['price']:.2f}\n"
            f"{rsi_line}"
            f"- Market sentiment: {sentiment}\n"
            f"- Recommendation: {decision}\n"
        )

    with open(os.path.join(vault, "wallet_balance.md"), "w", encoding="utf-8") as f:
        f.write("# Wallet Balance (paper)\n\n")
        f.write("| Agent | Budget | Equity | P/L |\n|---|---|---|---|\n")
        total_eq = 0.0
        for r in rows:
            if r["id"] != "PROD":
                total_eq += r["equity"]
            f.write(f"| {r['id']} | {r['budget']:.2f} | {r['equity']:.2f} | {r['pnl']:+.2f} |\n")
        f.write(f"\n**Total agent equity (excludes PROD book):** {total_eq:.2f}\n")

    with open(os.path.join(vault, "last_trade_log.md"), "w", encoding="utf-8") as f:
        f.write(
            f"# Last Trade Log (heartbeat)\n\n"
            f"- Last run: {snap['time']}\n"
            f"- Status: {status}\n"
            f"- Winning agent: {winner}\n"
            f"- Consensus decision: {decision}\n"
        )
