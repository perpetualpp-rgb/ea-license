"""Alert rules (CRUD), alert event history and acknowledgement.

Events are produced server-side (app/alerts_engine.py); members cannot create them.
The event list is delayed like every other market payload for delayed members, and
backfilled events are listed with is_backfill = true (they were never delivered live).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Literal, Optional

import psycopg
from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from ..alerts_engine import METRICS
from ..common import Ctx, DecimalIn, decode_cursor, dstr, encode_cursor, get_ctx, iso, resolve_access
from ..errors import ApiError, not_found

router = APIRouter(prefix="/v1/alerts", tags=["alerts"])

RULE_COLS = ("id, instrument_id, name, metric, comparator, threshold, threshold_unit, window_seconds,"
             " cooldown_seconds, channel, enabled, required_quality, created_at, updated_at")
EVENT_COLS = ("event_id, rule_id, dedupe_key, instrument_id, metric, comparator, observed_value, threshold, unit,"
              " window_start, window_end, observed_at, detected_at, source_watermark, engine_version, data_quality,"
              " is_backfill, delivery_policy, message, acknowledged_at")
Metric = Literal["cvd_change", "price_change_pct", "big_trade_qty", "oi_change_pct", "funding_rate", "liquidation_qty"]
Comparator = Literal["gt", "gte", "lt", "lte", "abs_gt"]


class RuleCreate(BaseModel):
    instrument_id: int = Field(description="asset + venue + market (one stored instrument)")
    name: Optional[str] = Field(None, max_length=120)
    metric: Metric
    comparator: Comparator = "gt"
    threshold: DecimalIn
    threshold_unit: Literal["base", "quote", "pct"]
    window_seconds: int = Field(300, ge=60, le=86400)
    cooldown_seconds: int = Field(900, ge=0, le=604800)
    channel: Literal["in_app", "telegram"] = Field("in_app", description="telegram is not available yet")
    enabled: bool = True
    required_quality: Literal["complete", "allow_partial"] = Field(
        "complete", description="observations below this quality pause the rule instead of alerting")


class RulePatch(BaseModel):
    name: Optional[str] = Field(None, max_length=120)
    comparator: Optional[Comparator] = None
    threshold: Optional[DecimalIn] = None
    threshold_unit: Optional[Literal["base", "quote", "pct"]] = None
    window_seconds: Optional[int] = Field(None, ge=60, le=86400)
    cooldown_seconds: Optional[int] = Field(None, ge=0, le=604800)
    channel: Optional[Literal["in_app", "telegram"]] = None
    enabled: Optional[bool] = None
    required_quality: Optional[Literal["complete", "allow_partial"]] = None


class Rule(BaseModel):
    id: uuid.UUID
    instrument_id: int
    name: Optional[str]
    metric: str
    comparator: str
    threshold: str
    threshold_unit: str
    window_seconds: int
    cooldown_seconds: int
    channel: str
    enabled: bool
    required_quality: str
    created_at: str
    updated_at: str


class Delivery(BaseModel):
    channel: str
    state: str
    state_reason: Optional[str]
    attempts: int
    created_at: str
    delivered_at: Optional[str]


class Event(BaseModel):
    event_id: uuid.UUID
    rule_id: uuid.UUID
    dedupe_key: str
    instrument_id: int
    metric: str
    comparator: str
    observed_value: str
    threshold: str
    unit: str
    window_start: str
    window_end: str
    observed_at: str
    detected_at: str
    source_watermark: Optional[str]
    engine_version: str
    data_quality: str
    is_backfill: bool
    delivery_policy: str
    message: str
    acknowledged_at: Optional[str]
    deliveries: Optional[List[Delivery]] = None


class EventPage(BaseModel):
    items: List[Event]
    next_cursor: Optional[str]


def _validate(conn: psycopg.Connection, instrument_id: int, metric: str, unit: str, channel: str) -> None:
    inst = conn.execute("SELECT market_type FROM instruments WHERE instrument_id = %s", [instrument_id]).fetchone()
    if inst is None:
        raise ApiError(422, "instrument_not_found", "instrument_id does not exist")
    m = METRICS[metric]
    if unit not in m["units"]:
        raise ApiError(422, "unit_mismatch", f"{metric} is measured in {', '.join(sorted(m['units']))}")
    if m["perp_only"] and inst["market_type"] not in ("perpetual", "future"):
        raise ApiError(422, "metric_not_available", f"{metric} exists only for perpetual/futures markets")
    if channel != "in_app":
        raise ApiError(422, "channel_not_available",
                       "only in_app is available; Telegram needs the member to link a chat first (not implemented)")


def rule_out(r: dict) -> dict:
    return {**r, "threshold": dstr(r["threshold"]), "created_at": iso(r["created_at"]), "updated_at": iso(r["updated_at"])}


def event_out(r: dict, deliveries: Optional[List[dict]] = None) -> dict:
    d = dict(r)
    for k in ("window_start", "window_end", "observed_at", "detected_at", "source_watermark", "acknowledged_at"):
        d[k] = iso(d[k])
    d["observed_value"], d["threshold"] = dstr(d["observed_value"]), dstr(d["threshold"])
    if deliveries is not None:
        d["deliveries"] = [{**x, "created_at": iso(x["created_at"]), "delivered_at": iso(x["delivered_at"])}
                           for x in deliveries]
    return d


@router.post("/rules", status_code=201, response_model=Rule, summary="Create an alert rule")
def create_rule(body: RuleCreate, ctx: Ctx = Depends(get_ctx)):
    with ctx.tx() as conn:
        _validate(conn, body.instrument_id, body.metric, body.threshold_unit, body.channel)
        row = conn.execute(
            f"INSERT INTO alert_rules (id, owner_id, instrument_id, name, metric, comparator, threshold, threshold_unit,"
            f" window_seconds, cooldown_seconds, channel, enabled, required_quality)"
            f" VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {RULE_COLS}",
            [uuid.uuid4(), ctx.user_id, body.instrument_id, body.name, body.metric, body.comparator, body.threshold,
             body.threshold_unit, body.window_seconds, body.cooldown_seconds, body.channel, body.enabled,
             body.required_quality]).fetchone()
    return rule_out(row)


@router.get("/rules", response_model=List[Rule], summary="List own alert rules")
def list_rules(ctx: Ctx = Depends(get_ctx)):
    with ctx.tx() as conn:
        rows = conn.execute(f"SELECT {RULE_COLS} FROM alert_rules WHERE owner_id = %s ORDER BY created_at, id",
                            [ctx.user_id]).fetchall()
    return [rule_out(r) for r in rows]


@router.get("/rules/{rule_id}", response_model=Rule, summary="One own alert rule")
def get_rule(rule_id: uuid.UUID, ctx: Ctx = Depends(get_ctx)):
    with ctx.tx() as conn:
        row = conn.execute(f"SELECT {RULE_COLS} FROM alert_rules WHERE id = %s AND owner_id = %s",
                           [rule_id, ctx.user_id]).fetchone()
    if row is None:
        raise not_found("alert rule")
    return rule_out(row)


@router.patch("/rules/{rule_id}", response_model=Rule, summary="Change an alert rule (events keep their own copy)")
def patch_rule(rule_id: uuid.UUID, body: RulePatch, ctx: Ctx = Depends(get_ctx)):
    provided = body.model_fields_set
    with ctx.tx() as conn:
        cur = conn.execute(f"SELECT {RULE_COLS} FROM alert_rules WHERE id = %s AND owner_id = %s FOR UPDATE",
                           [rule_id, ctx.user_id]).fetchone()
        if cur is None:
            raise not_found("alert rule")
        sets = {k: getattr(body, k) for k in provided}
        for k, v in sets.items():
            if v is None and k != "name":
                raise ApiError(422, "validation_error", f"{k} cannot be null")
        merged = {**cur, **sets}
        _validate(conn, merged["instrument_id"], merged["metric"], merged["threshold_unit"], merged["channel"])
        if sets:
            assign = ", ".join(f"{k} = %s" for k in sets)
            cur = conn.execute(f"UPDATE alert_rules SET {assign}, updated_at = now() WHERE id = %s AND owner_id = %s"
                               f" RETURNING {RULE_COLS}", [*sets.values(), rule_id, ctx.user_id]).fetchone()
    return rule_out(cur)


@router.delete("/rules/{rule_id}", status_code=204, summary="Delete a rule and its event history")
def delete_rule(rule_id: uuid.UUID, ctx: Ctx = Depends(get_ctx)):
    with ctx.tx() as conn:
        row = conn.execute("DELETE FROM alert_rules WHERE id = %s AND owner_id = %s RETURNING id",
                           [rule_id, ctx.user_id]).fetchone()
    if row is None:
        raise not_found("alert rule")
    return Response(status_code=204)


@router.get("/events", response_model=EventPage, summary="Own alert events, newest market time first")
def list_events(rule_id: Optional[uuid.UUID] = None,
                include_backfill: bool = Query(True, description="false hides events produced from backfilled data"),
                unacknowledged: bool = False,
                cursor: Optional[str] = None,
                limit: int = Query(50, ge=1, le=100),
                ctx: Ctx = Depends(get_ctx)):
    c_ts, c_id = decode_cursor(cursor) if cursor else (None, None)
    with ctx.tx() as conn:
        access = resolve_access(conn, ctx.settings)
        rows = conn.execute(
            f"SELECT {EVENT_COLS} FROM alert_events WHERE owner_id = %(o)s AND observed_at < %(cut)s"
            " AND (%(r)s::uuid IS NULL OR rule_id = %(r)s) AND (%(b)s OR NOT is_backfill)"
            " AND (NOT %(u)s OR acknowledged_at IS NULL)"
            " AND (%(cts)s::timestamptz IS NULL OR (observed_at, event_id) < (%(cts)s, %(cid)s::uuid))"
            " ORDER BY observed_at DESC, event_id DESC LIMIT %(lim)s",
            {"o": ctx.user_id, "cut": access.cutoff, "r": rule_id,
             "b": include_backfill, "u": unacknowledged, "cts": c_ts, "cid": c_id, "lim": limit + 1}).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    nxt = encode_cursor(rows[-1]["observed_at"], rows[-1]["event_id"]) if more and rows else None
    return {"items": [event_out(r) for r in rows], "next_cursor": nxt}


def _visible_event(conn: psycopg.Connection, ctx: Ctx, event_id: uuid.UUID) -> dict:
    access = resolve_access(conn, ctx.settings)
    row = conn.execute(f"SELECT {EVENT_COLS} FROM alert_events WHERE event_id = %s AND owner_id = %s"
                       " AND observed_at < %s", [event_id, ctx.user_id, access.cutoff]).fetchone()
    if row is None:
        raise not_found("alert event")
    return row


@router.get("/events/{event_id}", response_model=Event, summary="One event with its delivery history")
def get_event(event_id: uuid.UUID, ctx: Ctx = Depends(get_ctx)):
    with ctx.tx() as conn:
        row = _visible_event(conn, ctx, event_id)
        deliveries = conn.execute(
            "SELECT channel, state, state_reason, attempts, created_at, delivered_at FROM alert_deliveries"
            " WHERE event_id = %s AND owner_id = %s ORDER BY created_at", [event_id, ctx.user_id]).fetchall()
    return event_out(row, deliveries)


@router.post("/events/{event_id}/ack", response_model=Event, summary="Acknowledge an event (idempotent)")
def ack_event(event_id: uuid.UUID, ctx: Ctx = Depends(get_ctx)):
    with ctx.tx() as conn:
        _visible_event(conn, ctx, event_id)
        row = conn.execute(
            f"UPDATE alert_events SET acknowledged_at = coalesce(acknowledged_at, now())"
            f" WHERE event_id = %s AND owner_id = %s RETURNING {EVENT_COLS}", [event_id, ctx.user_id]).fetchone()
    return event_out(row)
