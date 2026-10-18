"""Server-side alert event recording (called by an evaluator worker, not by members).

record_alert_event() is idempotent on (rule_id, dedupe_key): the dedupe key is derived
from the rule and the observation window, so a reconnect, a retry or a re-evaluation by a
newer engine never notifies twice. Delivery policy, decided once when the event is
created:

* backfilled observations (flagged by the caller, or older than alert_max_live_lag_s)
  are stored with is_backfill = true and never delivered as live alerts;
* members without a realtime entitlement get no live delivery (their event list is also
  delayed by the API);
* a live event inside the rule's cooldown is stored but its delivery is suppressed;
* observations whose data quality the rule does not accept create no event at all (the
  rule is paused while its stream is broken).

The evaluator that computes observations from metric_events/trades and the workers that
push in-app/Telegram messages are not implemented yet.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from .common import MarketAccess, dstr, resolve_access, utcnow
from .config import ENGINE_VERSION, Settings
from .db import Database

ACCEPTED_QUALITY = {"complete": {"complete"}, "allow_partial": {"complete", "partial", "approximate"}}
METRICS = {
    "cvd_change": {"label": "CVD", "units": {"base", "quote"}, "perp_only": False},
    "price_change_pct": {"label": "ราคา", "units": {"pct"}, "perp_only": False},
    "big_trade_qty": {"label": "ไม้ใหญ่", "units": {"base", "quote"}, "perp_only": False},
    "oi_change_pct": {"label": "OI", "units": {"pct"}, "perp_only": True},
    "funding_rate": {"label": "Funding", "units": {"pct"}, "perp_only": True},
    "liquidation_qty": {"label": "Liquidation", "units": {"base", "quote"}, "perp_only": True},
}
OPS = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "abs_gt": "|x| >"}


@dataclass(frozen=True)
class Observation:
    value: Decimal
    window_start: datetime
    window_end: datetime
    observed_at: datetime
    data_quality: str                      # 'complete' | 'partial' | 'approximate' | 'gap'
    is_backfill: bool = False
    source_watermark: Optional[datetime] = None
    engine_version: str = ENGINE_VERSION


@dataclass
class EmitResult:
    outcome: str                           # 'created' | 'duplicate' | 'paused_data_quality' | 'rule_disabled' | 'not_triggered'
    event: Optional[dict] = None
    delivery_state: Optional[str] = None


def dedupe_key(rule_id: uuid.UUID, window_start: datetime, window_end: datetime) -> str:
    raw = f"{rule_id}|{window_start.isoformat()}|{window_end.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def triggered(comparator: str, value: Decimal, threshold: Decimal) -> bool:
    return {"gt": value > threshold, "gte": value >= threshold, "lt": value < threshold,
            "lte": value <= threshold, "abs_gt": abs(value) > threshold}[comparator]


def _window_label(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _unit_suffix(unit: str, base: str, quote: str) -> str:
    return {"pct": "%", "base": f" {base}", "quote": f" {quote}"}.get(unit, f" {unit}")


def format_message(rule: dict, value: Decimal, base: str, quote: str) -> str:
    """e.g. 'OI +2.1%/5m > เกณฑ์ 2%' -- the measured value and the threshold, never a forecast."""
    label = METRICS.get(rule["metric"], {}).get("label", rule["metric"])
    unit = _unit_suffix(rule["threshold_unit"], base, quote)
    sign = "+" if value > 0 else ""
    return (f"{label} {sign}{dstr(value)}{unit}/{_window_label(rule['window_seconds'])} "
            f"{OPS[rule['comparator']]} เกณฑ์ {dstr(rule['threshold'])}{unit}")


def record_alert_event(db: Database, settings: Settings, owner_id: uuid.UUID, rule_id: uuid.UUID,
                       obs: Observation, now: Optional[datetime] = None) -> EmitResult:
    now = now or utcnow()
    with db.user_tx(owner_id) as conn:
        rule = conn.execute(
            "SELECT r.*, i.base_asset, i.quote_asset FROM alert_rules r JOIN instruments i USING (instrument_id)"
            " WHERE r.id = %s AND r.owner_id = %s", [rule_id, owner_id]).fetchone()
        if rule is None:
            raise LookupError("rule not found for this owner")
        if not rule["enabled"]:
            return EmitResult("rule_disabled")
        if obs.data_quality not in ACCEPTED_QUALITY[rule["required_quality"]]:
            return EmitResult("paused_data_quality")
        if not triggered(rule["comparator"], obs.value, rule["threshold"]):
            return EmitResult("not_triggered")
        is_backfill = obs.is_backfill or (now - obs.observed_at) > timedelta(seconds=settings.alert_max_live_lag_s)
        access: MarketAccess = resolve_access(conn, settings, now)
        if is_backfill:
            policy = "not_delivered_backfill"
        elif access.level != "realtime":
            policy = "suppressed_entitlement"
        else:
            recent = conn.execute(
                "SELECT 1 FROM alert_events WHERE rule_id = %s AND owner_id = %s AND delivery_policy = 'live'"
                " AND observed_at > %s AND observed_at <= %s LIMIT 1",
                [rule_id, owner_id, obs.observed_at - timedelta(seconds=rule["cooldown_seconds"]),
                 obs.observed_at]).fetchone() if rule["cooldown_seconds"] else None
            policy = "suppressed_cooldown" if recent else "live"
        event_id = uuid.uuid4()
        row = conn.execute(
            """
            INSERT INTO alert_events (event_id, owner_id, rule_id, dedupe_key, instrument_id, metric, comparator,
                observed_value, threshold, unit, window_start, window_end, observed_at, detected_at, source_watermark,
                engine_version, data_quality, is_backfill, delivery_policy, message)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (rule_id, dedupe_key) DO NOTHING
            RETURNING *
            """,
            [event_id, owner_id, rule_id, dedupe_key(rule_id, obs.window_start, obs.window_end),
             rule["instrument_id"], rule["metric"], rule["comparator"], obs.value, rule["threshold"],
             rule["threshold_unit"], obs.window_start, obs.window_end, obs.observed_at, now, obs.source_watermark,
             obs.engine_version, obs.data_quality, is_backfill, policy,
             format_message(rule, obs.value, rule["base_asset"], rule["quote_asset"])]).fetchone()
        if row is None:
            existing = conn.execute("SELECT * FROM alert_events WHERE rule_id = %s AND dedupe_key = %s",
                                    [rule_id, dedupe_key(rule_id, obs.window_start, obs.window_end)]).fetchone()
            return EmitResult("duplicate", existing)
        state = None
        if policy != "not_delivered_backfill":
            state = "pending" if policy == "live" else "suppressed"
            conn.execute(
                "INSERT INTO alert_deliveries (id, owner_id, event_id, channel, state, state_reason, next_attempt_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [uuid.uuid4(), owner_id, event_id, rule["channel"], state, None if policy == "live" else policy,
                 now if policy == "live" else None])
        return EmitResult("created", row, state)
