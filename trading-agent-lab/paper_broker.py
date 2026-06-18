"""Simulated (paper) order execution and portfolio accounting.

NO REAL ORDERS ARE EVER PLACED. Every "trade" only mutates an in-memory /
on-disk portfolio. This is the single component you would replace to go live,
and doing so is intentionally left to the operator (see README).
"""
from __future__ import annotations

from typing import Dict

from agents import BUY, SELL


class PaperPortfolio:
    """Tracks cash + a single long BTC position funded from a fixed budget."""

    def __init__(self, budget: float, state: Dict | None = None):
        self.budget = budget
        if state:
            self.cash = state["cash"]
            self.qty = state["qty"]
            self.avg_price = state["avg_price"]
            self.realized = state["realized"]
        else:
            self.cash = budget
            self.qty = 0.0
            self.avg_price = 0.0
            self.realized = 0.0

    def to_state(self) -> Dict:
        return {
            "cash": self.cash,
            "qty": self.qty,
            "avg_price": self.avg_price,
            "realized": self.realized,
        }

    def equity(self, price: float) -> float:
        return self.cash + self.qty * price

    def pnl(self, price: float) -> float:
        """Total P/L vs the original budget (realized + unrealized)."""
        return self.equity(price) - self.budget

    def execute(self, action: str, price: float, fraction: float = 1.0) -> str:
        """Apply a paper order. Returns a short description of what happened."""
        if action == BUY and self.cash > 0:
            spend = self.cash * fraction
            bought = spend / price
            total_cost = self.avg_price * self.qty + spend
            self.qty += bought
            self.avg_price = total_cost / self.qty if self.qty else 0.0
            self.cash -= spend
            return f"BUY {bought:.6f} @ {price:.2f}"
        if action == SELL and self.qty > 0:
            proceeds = self.qty * price
            self.realized += proceeds - self.avg_price * self.qty
            sold = self.qty
            self.cash += proceeds
            self.qty = 0.0
            self.avg_price = 0.0
            return f"SELL {sold:.6f} @ {price:.2f}"
        return "no-op"
