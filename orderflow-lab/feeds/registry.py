"""Build a feed from a config entry: {"exchange": "okx", "symbol": "BTC-USDT"}."""
from __future__ import annotations

from .coinbase import CoinbaseFeed
from .kraken import KrakenFeed
from .okx import OKXFeed

FEEDS = {
    "okx": lambda cfg: OKXFeed(cfg["symbol"], float(cfg.get("contract_value", 1.0))),
    "kraken": lambda cfg: KrakenFeed(cfg["symbol"]),
    "coinbase": lambda cfg: CoinbaseFeed(cfg["symbol"]),
}


def make_feed(cfg: dict):
    try:
        return FEEDS[cfg["exchange"]](cfg)
    except KeyError as exc:
        raise ValueError(f"unknown exchange {cfg.get('exchange')!r}; known: {sorted(FEEDS)}") from exc
