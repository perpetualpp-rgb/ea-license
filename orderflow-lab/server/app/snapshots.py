"""Server-side market snapshots: the values are computed here from stored data, frozen in
market_snapshots, and never recomputed in place (a newer engine produces a new snapshot).

A delayed member's snapshot is clamped to the same cut-off as every market endpoint.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import List, Optional

import psycopg
from psycopg.types.json import Jsonb

from .common import MarketAccess, dstr, iso
from .config import ENGINE_VERSION, SNAPSHOT_SCHEMA_VERSION
from .errors import ApiError
from .routers.market import GAP_STATUSES_BAD, gaps_as_of, kline_summary, load_instrument, trade_summary, last_trade_price

LAYERS = {"price", "volume", "vwap", "poc", "cvd", "walls", "big_trades", "oi", "funding"}
MAX_VIEWPORT = timedelta(days=7)


def build_snapshot(conn: psycopg.Connection, owner_id: uuid.UUID, access: MarketAccess, *, instrument_id: int,
                   tf: str, viewport_from: datetime, viewport_to: datetime, layers: List[str], parameters: dict,
                   image_sha256: Optional[str]) -> dict:
    inst = load_instrument(conn, instrument_id)
    unknown = sorted(set(layers) - LAYERS)
    if unknown:
        raise ApiError(422, "unknown_layer", f"unknown chart layers: {', '.join(unknown)}")
    to = min(viewport_to, access.cutoff)
    if viewport_from >= to:
        raise ApiError(422, "viewport_after_data_cutoff",
                       "the viewport starts after the newest data this membership may see",
                       {"data_until": iso(access.cutoff)})
    if to - viewport_from > MAX_VIEWPORT:
        raise ApiError(422, "range_too_large", "a snapshot viewport covers at most 7 days")

    k = kline_summary(conn, instrument_id, viewport_from, to)
    t = trade_summary(conn, instrument_id, viewport_from, to)
    last_price = last_trade_price(conn, instrument_id, viewport_from, to)
    gaps = gaps_as_of(conn, instrument_id, viewport_from, to, access.cutoff)
    expected_minutes = int((to - viewport_from).total_seconds() // 60)
    has_taker = k["candles"] and k["candles_with_taker"] == k["candles"]
    vwap = (k["tpv"] / k["volume"]) if k["volume"] else None
    cvd = (t["buy"] or 0) - (t["sell"] or 0) if t["trades"] else None

    metric_values = {
        "kline_1m": {
            "candles": k["candles"], "open": dstr(k["open"]), "high": dstr(k["high"]), "low": dstr(k["low"]),
            "close": dstr(k["close"]), "volume": dstr(k["volume"]),
            "taker_buy_volume": dstr(k["taker_buy_volume"]) if has_taker else None,
            "taker_sell_volume": dstr(k["volume"] - k["taker_buy_volume"]) if has_taker else None,
            "volume_unit": inst["base_asset"], "price_unit": inst["quote_asset"],
            "vwap": {"value": dstr(vwap), "method": "typical price (h+l+c)/3 of 1m klines weighted by volume"},
        },
        "trade_tape": {
            "trades": t["trades"], "volume": dstr(t["volume"]),
            "taker_buy_volume": dstr(t["buy"]), "taker_sell_volume": dstr(t["sell"]),
            "unknown_side_volume": dstr(t["unknown"]),
            "cvd": dstr(cvd), "cvd_unit": inst["base_asset"], "cvd_anchor": iso(viewport_from),
            "cvd_method": "taker buy - taker sell volume from the trade tape; unknown side excluded",
            "last_price": {"value": dstr(last_price), "type": "last_trade"},
        },
    }
    newest = [x for x in (t["last_ts"], (k["last_open"] + timedelta(seconds=60)) if k["last_open"] else None) if x]
    bad_gaps = [g for g in gaps if g["status"] in GAP_STATUSES_BAD]
    quality_flags = {
        "no_data": not k["candles"] and not t["trades"],
        "kline_minutes_expected": expected_minutes,
        "kline_minutes_present": k["candles"],
        "kline_complete": k["candles"] == expected_minutes and expected_minutes > 0,
        "gap_ids": [g["id"] for g in gaps],
        "unresolved_gap_streams": sorted({g["stream"] for g in bad_gaps}),
        "delayed": access.level == "delayed",
        "clamped_to_cutoff": viewport_to > to,
    }
    sid = uuid.uuid4()
    row = conn.execute(
        """
        INSERT INTO market_snapshots (id, owner_id, instrument_id, captured_at, source_event_time, source_watermark,
            data_access, metric_values, parameters, engine_version, schema_version, quality_flags, chart_viewport,
            chart_layers, image_object_key, image_sha256)
        VALUES (%s, %s, %s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        [sid, owner_id, instrument_id, max(newest) if newest else None, to, access.level, Jsonb(metric_values),
         Jsonb(parameters), ENGINE_VERSION, SNAPSHOT_SCHEMA_VERSION, Jsonb(quality_flags),
         Jsonb({"tf": tf, "from": iso(viewport_from), "to": iso(to), "requested_to": iso(viewport_to)}),
         Jsonb(list(layers)), f"snapshots/{owner_id}/{sid}.png" if image_sha256 else None, image_sha256]).fetchone()
    return row


def snapshot_out(row: dict) -> dict:
    d = dict(row)
    for k in ("captured_at", "source_event_time", "source_watermark"):
        d[k] = iso(d[k])
    d["image_status"] = "upload_not_implemented" if d["image_object_key"] else "none"
    return d
