"""Market data read from the database (never from an exchange).

Every query takes the caller's MarketAccess: with a delayed entitlement the server only
reads rows older than now - FLOWLAB_DELAY_MINUTES (trades/book by event time, klines by
close time, gaps and metric events by market time), so delayed payloads are delayed in
full, not just hidden on screen.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional

import psycopg
from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime, BaseModel, Field

from ..common import Ctx, MarketAccess, dstr, get_ctx, iso, require_market_access, resolve_access, check_range
from ..errors import ApiError, not_found

router = APIRouter(prefix="/v1/instruments", tags=["market data"])

TIMEFRAMES = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
ORIGIN = datetime(2000, 1, 3, tzinfo=timezone.utc)  # a Monday 00:00 UTC; bars are bucketed in UTC
MAX_BARS = 5000
MAX_TRADE_SOURCE_RANGE = timedelta(days=1)
MAX_QUALITY_RANGE = timedelta(days=31)
GAP_STATUSES_BAD = ("open", "partial", "unrecoverable")


# -- models --------------------------------------------------------------------------
class Instrument(BaseModel):
    instrument_id: int
    venue: str
    symbol: str
    market_type: str
    contract_type: str
    base_asset: str
    quote_asset: str
    settlement_asset: str
    contract_multiplier: str
    multiplier_unit: str
    tick_size: Optional[str]
    qty_step: Optional[str]
    session_timezone: str
    spec_status: str
    metadata_version: int
    latest_trade_at: Optional[str] = None
    latest_kline_close_at: Optional[str] = None
    latest_book_at: Optional[str] = None
    source_notice: str = "แหล่งข้อมูลแสดงตามความจำเป็น ไม่ได้สื่อว่าเป็นพันธมิตรหรือได้รับการรับรอง"


class Access(BaseModel):
    level: str
    delay_minutes: int
    data_until: Optional[str]
    valid_until: Optional[str]


class InstrumentList(BaseModel):
    access: Access
    items: List[Instrument]


class Gap(BaseModel):
    id: int
    stream: str
    reason: str
    status: str
    gap_start: Optional[str]
    gap_end: Optional[str]
    first_missing_id: Optional[int]
    last_missing_id: Optional[int]
    expected_count: Optional[int]
    present_count: Optional[int]
    detected_at: Optional[str]
    repaired_at: Optional[str]


class Bar(BaseModel):
    t: str
    open: Optional[str]
    high: Optional[str]
    low: Optional[str]
    close: Optional[str]
    volume: Optional[str]
    taker_buy_volume: Optional[str]
    taker_sell_volume: Optional[str]
    unknown_side_volume: Optional[str]
    delta: Optional[str]
    trades: Optional[int]
    complete: bool


class BarsResponse(BaseModel):
    instrument_id: int
    tf: str
    source: str
    method: str
    units: Dict[str, str]
    access: Access
    from_: str = Field(serialization_alias="from")
    to: str
    bars: List[Bar]
    gaps: List[Gap]


class StreamCoverage(BaseModel):
    present: int
    expected: Optional[int]
    coverage_pct: Optional[str]
    has_data: bool
    first: Optional[str] = None
    last: Optional[str] = None


class QualityResponse(BaseModel):
    instrument_id: int
    access: Access
    from_: str = Field(serialization_alias="from")
    to: str
    definition: str
    streams: Dict[str, StreamCoverage]
    gaps: List[Gap]


class MetricEvent(BaseModel):
    id: int
    metric_type: str
    window_start: str
    window_end: str
    source_watermark: str
    session_anchor: Optional[str]
    unit: str
    value: Optional[str]
    parameters: Dict[str, Any]
    engine_version: str
    approximate: bool
    partial: bool
    revision: int
    supersedes_id: Optional[int]
    evidence_ref: Dict[str, Any]


class MetricEventList(BaseModel):
    access: Access
    items: List[MetricEvent]


# -- helpers (also used by snapshots) ------------------------------------------------------
INSTRUMENT_COLS = ("instrument_id, venue, symbol, market_type, contract_type, base_asset, quote_asset, settlement_asset,"
                   " contract_multiplier, multiplier_unit, tick_size, qty_step, session_timezone, spec_status,"
                   " metadata_version")


def instrument_dict(row: dict) -> dict:
    d = dict(row)
    for k in ("contract_multiplier", "tick_size", "qty_step"):
        d[k] = dstr(d[k])
    return d


def load_instrument(conn: psycopg.Connection, instrument_id: int) -> dict:
    row = conn.execute(f"SELECT {INSTRUMENT_COLS} FROM instruments WHERE instrument_id = %s", [instrument_id]).fetchone()
    if row is None:
        raise not_found("instrument")
    return row


def floor_bucket(t: datetime, step: int) -> datetime:
    n = int((t - ORIGIN).total_seconds() // step)
    return ORIGIN + timedelta(seconds=n * step)


def gaps_as_of(conn: psycopg.Connection, instrument_id: int, frm: datetime, to: datetime, cutoff: datetime,
               streams: Optional[List[str]] = None) -> List[dict]:
    """Gaps overlapping [frm, to) as they stood at ``cutoff`` (nothing newer is revealed)."""
    rows = conn.execute(
        """
        SELECT id, stream, reason,
               CASE WHEN repaired_at > %(cut)s THEN 'open' ELSE status END AS status,
               gap_start,
               CASE WHEN gap_end > %(cut)s THEN NULL ELSE gap_end END AS gap_end,
               first_missing_id, last_missing_id, expected_count,
               CASE WHEN repaired_at > %(cut)s THEN NULL ELSE present_count END AS present_count,
               CASE WHEN detected_at > %(cut)s THEN NULL ELSE detected_at END AS detected_at,
               CASE WHEN repaired_at > %(cut)s THEN NULL ELSE repaired_at END AS repaired_at
        FROM data_quality
        WHERE instrument_id = %(id)s
          AND gap_start IS NOT NULL AND gap_start < %(to)s AND gap_start < %(cut)s
          AND (gap_end IS NULL OR gap_end > %(frm)s)
          AND (%(streams)s::text[] IS NULL OR stream = ANY(%(streams)s::text[]))
        ORDER BY gap_start, id
        """, {"id": instrument_id, "frm": frm, "to": to, "cut": cutoff, "streams": streams}).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        for k in ("gap_start", "gap_end", "detected_at", "repaired_at"):
            r[k] = iso(r[k])
        out.append(r)
    return out


def kline_summary(conn: psycopg.Connection, instrument_id: int, frm: datetime, to: datetime) -> dict:
    return conn.execute(
        """
        SELECT count(*) AS candles, (array_agg(open ORDER BY open_time))[1] AS open, max(high) AS high,
               min(low) AS low, (array_agg(close ORDER BY open_time DESC))[1] AS close,
               sum(volume) AS volume, sum(taker_buy_volume) AS taker_buy_volume,
               count(taker_buy_volume) AS candles_with_taker,
               sum((high + low + close) / 3 * volume) AS tpv, max(open_time) AS last_open
        FROM klines WHERE instrument_id = %s AND interval_s = 60
          AND open_time >= %s AND open_time + interval '60 seconds' <= %s
        """, [instrument_id, frm, to]).fetchone()


def trade_summary(conn: psycopg.Connection, instrument_id: int, frm: datetime, to: datetime) -> dict:
    return conn.execute(
        """
        SELECT count(*) AS trades, sum(qty) AS volume,
               sum(qty) FILTER (WHERE taker_side = 'buy') AS buy, sum(qty) FILTER (WHERE taker_side = 'sell') AS sell,
               sum(qty) FILTER (WHERE taker_side = 'unknown') AS unknown,
               max(event_ts) AS last_ts, min(agg_trade_id) AS first_id, max(agg_trade_id) AS last_id
        FROM trades WHERE instrument_id = %s AND event_ts >= %s AND event_ts < %s
        """, [instrument_id, frm, to]).fetchone()


def last_trade_price(conn: psycopg.Connection, instrument_id: int, frm: datetime, to: datetime) -> Optional[Decimal]:
    row = conn.execute("SELECT price FROM trades WHERE instrument_id = %s AND event_ts >= %s AND event_ts < %s"
                       " ORDER BY event_ts DESC, agg_trade_id DESC LIMIT 1", [instrument_id, frm, to]).fetchone()
    return row["price"] if row else None


def _access(conn: psycopg.Connection, ctx: Ctx) -> MarketAccess:
    return require_market_access(resolve_access(conn, ctx.settings))


# -- endpoints ----------------------------------------------------------------------------
@router.get("", response_model=InstrumentList, summary="Instruments with the newest data time the caller may see")
def list_instruments(ctx: Ctx = Depends(get_ctx)):
    with ctx.tx() as conn:
        access = _access(conn, ctx)
        rows = conn.execute(f"SELECT {INSTRUMENT_COLS} FROM instruments ORDER BY venue, market_type, symbol").fetchall()
        items = []
        for r in rows:
            d = instrument_dict(r)
            iid = r["instrument_id"]
            t = conn.execute("SELECT max(event_ts) AS t FROM trades WHERE instrument_id = %s AND event_ts < %s",
                             [iid, access.cutoff]).fetchone()["t"]
            k = conn.execute("SELECT max(open_time) + interval '60 seconds' AS t FROM klines WHERE instrument_id = %s"
                             " AND interval_s = 60 AND open_time + interval '60 seconds' <= %s",
                             [iid, access.cutoff]).fetchone()["t"]
            b = conn.execute("SELECT max(captured_ts) AS t FROM book_snapshots WHERE instrument_id = %s"
                             " AND captured_ts < %s", [iid, access.cutoff]).fetchone()["t"]
            d.update(latest_trade_at=iso(t), latest_kline_close_at=iso(k), latest_book_at=iso(b))
            items.append(d)
    return {"access": access.as_dict(), "items": items}


@router.get("/{instrument_id}", response_model=Instrument, summary="One instrument and its contract specification")
def get_instrument(instrument_id: int, ctx: Ctx = Depends(get_ctx)):
    with ctx.tx() as conn:
        _access(conn, ctx)
        return instrument_dict(load_instrument(conn, instrument_id))


@router.get("/{instrument_id}/bars", response_model=BarsResponse,
            summary="OHLCV bars from stored 1m klines or from the trade tape")
def get_bars(instrument_id: int,
             tf: Literal["1m", "5m", "15m", "1h", "4h", "1d"] = "1m",
             from_: Optional[AwareDatetime] = Query(None, alias="from"),
             to: Optional[AwareDatetime] = None,
             source: Literal["kline", "trades"] = Query(
                 "kline", description="kline = exchange 1m candles; trades = aggregated trade tape with taker side"),
             ctx: Ctx = Depends(get_ctx)):
    check_range(from_, to)
    step = TIMEFRAMES[tf]
    with ctx.tx() as conn:
        access = _access(conn, ctx)
        inst = load_instrument(conn, instrument_id)
        to_eff = min(to or access.now, access.cutoff)
        frm = floor_bucket(from_ or (to_eff - timedelta(seconds=step * 500)), step)
        if (to_eff - frm).total_seconds() / step > MAX_BARS:
            raise ApiError(422, "range_too_large", f"at most {MAX_BARS} bars per request")
        if source == "trades" and to_eff - frm > MAX_TRADE_SOURCE_RANGE:
            raise ApiError(422, "range_too_large", "source=trades is limited to 1 day per request")
        bars: List[dict] = []
        if frm < to_eff:
            if source == "kline":
                rows = conn.execute(
                    """
                    SELECT date_bin(make_interval(secs => %(step)s), open_time, %(origin)s) AS t,
                           (array_agg(open ORDER BY open_time))[1] AS open, max(high) AS high, min(low) AS low,
                           (array_agg(close ORDER BY open_time DESC))[1] AS close,
                           sum(volume) AS volume, sum(taker_buy_volume) AS taker_buy_volume,
                           count(taker_buy_volume) AS with_taker, sum(trade_count) AS trades, count(*) AS candles
                    FROM klines
                    WHERE instrument_id = %(id)s AND interval_s = 60
                      AND open_time >= %(frm)s AND open_time + interval '60 seconds' <= %(to)s
                    GROUP BY 1 ORDER BY 1
                    """, {"step": step, "origin": ORIGIN, "id": instrument_id, "frm": frm, "to": to_eff}).fetchall()
                for r in rows:
                    sell = (r["volume"] - r["taker_buy_volume"]) if r["with_taker"] == r["candles"] else None
                    buy = r["taker_buy_volume"] if r["with_taker"] == r["candles"] else None
                    end = r["t"] + timedelta(seconds=step)
                    bars.append({
                        "t": iso(r["t"]), "open": dstr(r["open"]), "high": dstr(r["high"]), "low": dstr(r["low"]),
                        "close": dstr(r["close"]), "volume": dstr(r["volume"]),
                        "taker_buy_volume": dstr(buy), "taker_sell_volume": dstr(sell), "unknown_side_volume": None,
                        "delta": dstr(buy - sell) if buy is not None and sell is not None else None,
                        "trades": r["trades"], "complete": r["candles"] == step // 60 and end <= to_eff})
                method = "exchange 1m klines bucketed with date_bin in UTC; delta = taker buy - taker sell volume"
                gap_streams = ["kline"]
            else:
                rows = conn.execute(
                    """
                    SELECT date_bin(make_interval(secs => %(step)s), event_ts, %(origin)s) AS t,
                           (array_agg(price ORDER BY event_ts, agg_trade_id))[1] AS open, max(price) AS high,
                           min(price) AS low, (array_agg(price ORDER BY event_ts DESC, agg_trade_id DESC))[1] AS close,
                           sum(qty) AS volume,
                           coalesce(sum(qty) FILTER (WHERE taker_side = 'buy'), 0) AS buy,
                           coalesce(sum(qty) FILTER (WHERE taker_side = 'sell'), 0) AS sell,
                           coalesce(sum(qty) FILTER (WHERE taker_side = 'unknown'), 0) AS unknown,
                           count(*) AS trades
                    FROM trades WHERE instrument_id = %(id)s AND event_ts >= %(frm)s AND event_ts < %(to)s
                    GROUP BY 1 ORDER BY 1
                    """, {"step": step, "origin": ORIGIN, "id": instrument_id, "frm": frm, "to": to_eff}).fetchall()
                trade_gaps = [g for g in gaps_as_of(conn, instrument_id, frm, to_eff, access.cutoff, ["trades"])
                              if g["status"] in GAP_STATUSES_BAD]
                for r in rows:
                    end = r["t"] + timedelta(seconds=step)
                    hit = any(_overlaps(g, r["t"], end) for g in trade_gaps)
                    bars.append({
                        "t": iso(r["t"]), "open": dstr(r["open"]), "high": dstr(r["high"]), "low": dstr(r["low"]),
                        "close": dstr(r["close"]), "volume": dstr(r["volume"]),
                        "taker_buy_volume": dstr(r["buy"]), "taker_sell_volume": dstr(r["sell"]),
                        "unknown_side_volume": dstr(r["unknown"]), "delta": dstr(r["buy"] - r["sell"]),
                        "trades": r["trades"], "complete": end <= to_eff and not hit})
                method = ("aggregated trades bucketed in UTC; delta = taker buy - taker sell; "
                          "trades with unknown taker side are reported separately, never counted as sells")
                gap_streams = ["trades"]
        else:
            method, gap_streams = "no data before the entitlement cut-off", ["kline", "trades"]
        gaps = gaps_as_of(conn, instrument_id, frm, to_eff, access.cutoff, gap_streams)
    return {"instrument_id": instrument_id, "tf": tf, "source": source, "method": method,
            "units": {"price": inst["quote_asset"], "volume": inst["base_asset"]},
            "access": access.as_dict(), "from_": iso(frm), "to": iso(to_eff), "bars": bars, "gaps": gaps}


def _overlaps(g: dict, start: datetime, end: datetime) -> bool:
    gs = datetime.fromisoformat(g["gap_start"].replace("Z", "+00:00")) if g["gap_start"] else None
    ge = datetime.fromisoformat(g["gap_end"].replace("Z", "+00:00")) if g["gap_end"] else None
    return (gs is None or gs < end) and (ge is None or ge > start)


@router.get("/{instrument_id}/quality", response_model=QualityResponse,
            summary="Per-stream coverage of a window and the gaps inside it")
def get_quality(instrument_id: int,
                from_: Optional[AwareDatetime] = Query(None, alias="from"),
                to: Optional[AwareDatetime] = None,
                ctx: Ctx = Depends(get_ctx)):
    check_range(from_, to)
    with ctx.tx() as conn:
        access = _access(conn, ctx)
        load_instrument(conn, instrument_id)
        to_eff = min(to or access.now, access.cutoff)
        frm = from_ or (to_eff - timedelta(days=1))
        if to_eff - frm > MAX_QUALITY_RANGE:
            raise ApiError(422, "range_too_large", "at most 31 days per request")
        streams: Dict[str, dict] = {}
        if frm < to_eff:
            tr = trade_summary(conn, instrument_id, frm, to_eff)
            span = (tr["last_id"] - tr["first_id"] + 1) if tr["trades"] else None
            streams["trades"] = {"present": tr["trades"], "expected": span, "has_data": tr["trades"] > 0,
                                 "coverage_pct": _pct(tr["trades"], span),
                                 "first": str(tr["first_id"]) if tr["first_id"] is not None else None,
                                 "last": str(tr["last_id"]) if tr["last_id"] is not None else None}
            secs = conn.execute(
                "SELECT count(DISTINCT date_trunc('second', captured_ts)) AS n, min(captured_ts) AS a,"
                " max(captured_ts) AS b FROM book_snapshots"
                " WHERE instrument_id = %s AND kind = 'bucket_summary' AND captured_ts >= %s AND captured_ts < %s",
                [instrument_id, frm, to_eff]).fetchone()
            expected_secs = int((to_eff - frm).total_seconds())
            streams["book"] = {"present": secs["n"], "expected": expected_secs, "has_data": secs["n"] > 0,
                               "coverage_pct": _pct(secs["n"], expected_secs),
                               "first": iso(secs["a"]), "last": iso(secs["b"])}
            first_min = floor_bucket(frm, 60) + (timedelta(0) if floor_bucket(frm, 60) == frm else timedelta(minutes=1))
            expected_min = max(0, int((floor_bucket(to_eff, 60) - first_min).total_seconds() // 60))
            km = conn.execute(
                "SELECT count(*) AS n, min(open_time) AS a, max(open_time) AS b FROM klines WHERE instrument_id = %s"
                " AND interval_s = 60 AND open_time >= %s AND open_time + interval '60 seconds' <= %s",
                [instrument_id, frm, to_eff]).fetchone()
            streams["kline"] = {"present": km["n"], "expected": expected_min, "has_data": km["n"] > 0,
                                "coverage_pct": _pct(km["n"], expected_min), "first": iso(km["a"]), "last": iso(km["b"])}
        else:
            for s in ("trades", "book", "kline"):
                streams[s] = {"present": 0, "expected": 0, "has_data": False, "coverage_pct": None}
        gaps = gaps_as_of(conn, instrument_id, frm, to_eff, access.cutoff)
    return {"instrument_id": instrument_id, "access": access.as_dict(), "from_": iso(frm), "to": iso(to_eff),
            "definition": ("coverage = rows present / rows expected in the requested window (book: seconds, "
                           "kline: closed minutes, trades: aggregate ids between the first and last id seen); "
                           "a window without data reports 0% and has_data=false, never 100%"),
            "streams": streams, "gaps": gaps}


def _pct(present: int, expected: Optional[int]) -> Optional[str]:
    if not expected:
        return None
    return dstr((Decimal(present) * 100 / Decimal(expected)).quantize(Decimal("0.01")))


@router.get("/{instrument_id}/metric-events", response_model=MetricEventList,
            summary="Versioned derived metrics/events (windows older than the entitlement cut-off)")
def get_metric_events(instrument_id: int,
                      from_: Optional[AwareDatetime] = Query(None, alias="from"),
                      to: Optional[AwareDatetime] = None,
                      metric_type: Optional[str] = None,
                      ctx: Ctx = Depends(get_ctx)):
    check_range(from_, to)
    with ctx.tx() as conn:
        access = _access(conn, ctx)
        load_instrument(conn, instrument_id)
        to_eff = min(to or access.now, access.cutoff)
        frm = from_ or (to_eff - timedelta(days=1))
        rows = conn.execute(
            "SELECT id, metric_type, window_start, window_end, source_watermark, session_anchor, unit, value,"
            " parameters, engine_version, approximate, partial, revision, supersedes_id, evidence_ref"
            " FROM metric_events WHERE instrument_id = %s AND window_end <= %s AND window_end > %s"
            " AND source_watermark <= %s AND (%s::text IS NULL OR metric_type = %s)"
            " ORDER BY window_end, id LIMIT 5000",
            [instrument_id, to_eff, frm, access.cutoff, metric_type, metric_type]).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        for k in ("window_start", "window_end", "source_watermark", "session_anchor"):
            d[k] = iso(d[k])
        d["value"] = dstr(d["value"])
        items.append(d)
    return {"access": access.as_dict(), "items": items}
