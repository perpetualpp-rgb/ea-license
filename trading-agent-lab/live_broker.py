"""Real-exchange order execution via ccxt — the opt-in "go-live" seam.

This is the component the paper lab deliberately did *not* ship. It places
**real orders with real money** when fully armed, so it is wrapped in several
independent safety gates and defaults to the safest possible behaviour:

    testnet/sandbox = True   and   dry_run = True

Nothing here fires a live order unless the operator explicitly turns every gate
off. The class mirrors ``paper_broker.PaperPortfolio``'s method surface
(``execute`` / ``equity`` / ``pnl`` / ``to_state``) so the Flows Agent's execute
node can use either one interchangeably.

Safety gates (ALL must pass for a *real* order to be sent):
  1. config ``live_trading: true``         — master kill-switch
  2. config ``broker.mode: "live"``         — opt into the live broker
  3. ``--live`` flag (or ``ALLOW_LIVE=1``)  — per-invocation arming
  4. API key + secret present in env        — never stored in the repo
  5. ``broker.dry_run`` is false            — otherwise log-only
  6. ``broker.testnet`` controls sandbox vs real venue (defaults to sandbox)
  7. per-order notional <= ``max_order_usd`` and the daily-loss kill-switch

ccxt is imported lazily so the rest of the lab still runs with no dependency.
"""
from __future__ import annotations

import os
from typing import Dict, Optional

from agents import BUY, SELL

DEFAULTS = {
    "exchange": "binance",
    "testnet": True,
    "dry_run": True,
    "api_key_env": "EXCHANGE_API_KEY",
    "api_secret_env": "EXCHANGE_API_SECRET",
    "max_order_usd": 50.0,
    "max_daily_loss_usd": 100.0,
}


class LiveTradingError(RuntimeError):
    """Raised when live trading is requested but a safety gate is not satisfied."""


def _split_symbol(symbol: str) -> str:
    """Turn a feed symbol like ``BTCUSDT`` into a ccxt market like ``BTC/USDT``."""
    for quote in ("USDT", "USDC", "USD", "BUSD"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[:-len(quote)]}/{quote}"
    return symbol


class LiveBroker:
    """ccxt-backed broker. Defaults to sandbox + dry-run; arms only on demand."""

    def __init__(self, symbol: str, cfg: Dict, allow_live: bool):
        c = {**DEFAULTS, **(cfg or {})}
        self.symbol = symbol
        self.market = _split_symbol(symbol)
        self.exchange_id = str(c["exchange"])
        self.testnet = bool(c["testnet"])
        self.dry_run = bool(c["dry_run"])
        self.max_order_usd = float(c["max_order_usd"])
        self.max_daily_loss_usd = float(c["max_daily_loss_usd"])
        self.allow_live = bool(allow_live)
        self.api_key = os.environ.get(str(c["api_key_env"]), "").strip()
        self.api_secret = os.environ.get(str(c["api_secret_env"]), "").strip()
        self._exchange = None  # lazily built ccxt client

    # --- arming / status -------------------------------------------------
    @property
    def armed(self) -> bool:
        """True only when every gate for sending a REAL order is satisfied."""
        return (self.allow_live and not self.dry_run
                and bool(self.api_key) and bool(self.api_secret))

    def status(self) -> str:
        venue = f"{self.exchange_id}{' (testnet)' if self.testnet else ' (LIVE VENUE)'}"
        if self.dry_run:
            return f"LIVE broker DRY-RUN on {venue} — orders are logged, not sent"
        if not self.allow_live:
            return f"LIVE broker configured for {venue} but not armed (--live missing)"
        if not (self.api_key and self.api_secret):
            return f"LIVE broker for {venue} but API key/secret env not set -> refused"
        return f"!!! ARMED: sending REAL orders to {venue} (market {self.market}) !!!"

    # --- ccxt client -----------------------------------------------------
    def _client(self):
        if self._exchange is not None:
            return self._exchange
        try:
            import ccxt  # type: ignore
        except Exception as e:  # pragma: no cover - depends on optional dep
            raise LiveTradingError(
                "ccxt is not installed. `pip install ccxt` to trade live.") from e
        klass = getattr(ccxt, self.exchange_id, None)
        if klass is None:
            raise LiveTradingError(f"unknown ccxt exchange: {self.exchange_id}")
        ex = klass({"apiKey": self.api_key, "secret": self.api_secret,
                    "enableRateLimit": True})
        if self.testnet:
            try:
                ex.set_sandbox_mode(True)
            except Exception as e:  # pragma: no cover
                raise LiveTradingError(
                    f"{self.exchange_id} has no sandbox/testnet support in ccxt; "
                    "refusing to fall back to the live venue.") from e
        self._exchange = ex
        return ex

    # --- balances / equity ----------------------------------------------
    def _balances(self) -> Dict[str, float]:
        base, quote = self.market.split("/")
        if self.dry_run or not self.armed:
            return {"base": 0.0, "quote": 0.0, base: 0.0, quote: 0.0}
        bal = self._client().fetch_balance()
        return {"base": float(bal.get(base, {}).get("free", 0.0) or 0.0),
                "quote": float(bal.get(quote, {}).get("free", 0.0) or 0.0)}

    def equity(self, price: float) -> float:
        b = self._balances()
        return b["quote"] + b["base"] * price

    def pnl(self, price: float) -> float:
        # Live PnL is session-relative; the execute node persists the baseline.
        return 0.0

    def to_state(self) -> Dict:
        return {}

    # --- the order seam --------------------------------------------------
    def execute(self, action: str, price: float, fraction: float = 1.0) -> str:
        """Place (or simulate) a market order. Honours every safety gate."""
        if action not in (BUY, SELL):
            return "no-op"

        if action == BUY:
            notional = min(self.max_order_usd, self._balances()["quote"] * fraction
                           if self.armed else self.max_order_usd * fraction)
            notional = min(notional, self.max_order_usd)
            amount = notional / price if price else 0.0
        else:  # SELL the whole base position
            amount = self._balances()["base"] if self.armed else (self.max_order_usd / price)
            notional = amount * price

        if amount <= 0:
            return "no-op (nothing to trade)"

        # Hard per-order notional cap (defence in depth).
        if notional > self.max_order_usd + 1e-9:
            return f"REFUSED: {action} notional ${notional:.2f} > cap ${self.max_order_usd:.2f}"

        if not self.armed:
            why = ("dry-run" if self.dry_run else
                   "not armed (--live)" if not self.allow_live else "no API key")
            return f"DRY {action} {amount:.6f} {self.market} (~${notional:.2f}) [{why}]"

        side = "buy" if action == BUY else "sell"
        order = self._client().create_order(self.market, "market", side, amount)
        oid = order.get("id", "?") if isinstance(order, dict) else "?"
        return f"LIVE {action} {amount:.6f} {self.market} (~${notional:.2f}) id={oid}"
