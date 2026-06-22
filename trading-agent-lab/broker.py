"""Broker selection + the live-trading safety gates, in one place.

``get_broker`` returns either the simulated ``PaperPortfolio`` (default, safe)
or the real ``LiveBroker`` — but only after every gate in ``live_broker`` is
satisfied. The daily-loss kill-switch lives here too: once realised losses for
the UTC day exceed ``max_daily_loss_usd`` the broker is forced back to paper for
the rest of the day, regardless of configuration.
"""
from __future__ import annotations

import datetime as _dt
from typing import Dict, Tuple

from live_broker import LiveBroker, LiveTradingError
from paper_broker import PaperPortfolio


def _today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def daily_loss_tripped(state: Dict, limit: float) -> bool:
    """True when today's realised loss has hit the kill-switch limit."""
    led = state.get("daily_loss", {})
    return led.get("date") == _today() and float(led.get("loss", 0.0)) >= limit


def record_realized(state: Dict, delta: float) -> None:
    """Accumulate realised PnL into today's bucket (losses are positive here)."""
    led = state.get("daily_loss", {})
    if led.get("date") != _today():
        led = {"date": _today(), "loss": 0.0}
    if delta < 0:
        led["loss"] = float(led.get("loss", 0.0)) + (-delta)
    state["daily_loss"] = led


def get_broker(symbol: str, cfg: Dict, state: Dict, allow_live: bool) -> Tuple[object, str]:
    """Return ``(broker, mode_label)``. Falls back to paper unless fully armed.

    Live requires, together: ``live_trading: true`` (master switch),
    ``broker.mode == "live"``, ``allow_live`` (the --live flag / ALLOW_LIVE=1),
    and the broker's own API-key/dry-run gates. Any failure -> paper, with a
    label explaining why, so callers can surface it.
    """
    broker_cfg = cfg.get("broker", {})
    budget = sum(a["budget"] for a in cfg["agents"])

    wants_live = bool(cfg.get("live_trading")) and broker_cfg.get("mode") == "live"
    if not wants_live:
        return PaperPortfolio(budget, state.get("prod")), "paper"

    limit = float(broker_cfg.get("max_daily_loss_usd", 100.0))
    if daily_loss_tripped(state, limit):
        return (PaperPortfolio(budget, state.get("prod")),
                f"paper (daily-loss kill-switch tripped: >= ${limit:.2f})")

    try:
        live = LiveBroker(symbol, broker_cfg, allow_live)
    except LiveTradingError as e:
        return PaperPortfolio(budget, state.get("prod")), f"paper ({e})"

    # Armed = real orders; otherwise the live broker is in dry-run/unarmed mode,
    # which is still safe to use (it logs instead of sending).
    return live, ("live-armed" if live.armed else f"live-dryrun ({live.status()})")
