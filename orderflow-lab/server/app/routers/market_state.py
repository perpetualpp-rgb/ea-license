"""Quote, details and observations of one instrument (read from our database only).

Same entitlement rule as /bars: every row used is older than the member's cut-off
(``access.data_until``; now - 15 min for delayed members), enforced in the queries.
Calculations live in ``app.orderflow`` (engine walls/profile/big-trade threshold).
"""
from __future__ import annotations

from datetime import timedelta
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime, BaseModel, Field

from .. import orderflow as of
from ..common import Ctx, MarketAccess, get_ctx, iso, require_market_access, resolve_access, check_range
from ..errors import ApiError
from .market import Access, load_instrument

router = APIRouter(prefix="/v1/instruments", tags=["market data"])

DETAILS_DEFAULT_RANGE = timedelta(hours=6)
DETAILS_MAX_RANGE = timedelta(days=3)


# -- models --------------------------------------------------------------------------
class LastTrade(BaseModel):
    price: str
    at: str
    source: Literal["trade", "kline"] = Field(description="trade = newest trade; kline = newest closed 1m kline close")


class MarkQuote(BaseModel):
    price: str
    index: Optional[str]
    at: str


class Change24h(BaseModel):
    pct: str = Field(description="(price / ref_price - 1) x 100, two decimals")
    ref_price: str
    ref_at: str = Field(description="close time of the 1m kline 24 h (+-5 min) before last_trade.at")


class SparkPoint(BaseModel):
    t: str
    close: str


class QuoteResponse(BaseModel):
    instrument_id: int
    access: Access
    last_trade: Optional[LastTrade]
    mark: Optional[MarkQuote] = Field(description="null for spot or when no mark row was collected")
    change_24h: Optional[Change24h] = Field(description="null when no 1m kline closed 24 h (+-5 min) before")
    spark: List[SparkPoint] = Field(description="1m kline closes every 30 min over the 24 h before last_trade.at, "
                                                "oldest first, at most 49")


class Wall(BaseModel):
    side: Literal["bid", "ask"]
    price: str
    size: str
    mult: str = Field(description="size / median size of the nearest levels on that side")
    age_s: int
    status: Literal["new", "holding", "adding", "pulling"]


class WallEvent(BaseModel):
    side: Literal["bid", "ask"]
    price: str
    size: str
    outcome: Literal["price_reached", "removed", "unknown"]
    at: str


class BookState(BaseModel):
    at: str
    synced: bool
    walls: List[Wall]
    events: List[WallEvent]
    method: str


class BigTrade(BaseModel):
    at: str
    side: Literal["buy", "sell"] = Field(description="taker side")
    price: str
    size: str


class BigTrades(BaseModel):
    threshold: Optional[str]
    items: List[BigTrade]
    method: str


class ProfileState(BaseModel):
    bin_size: str
    poc: Optional[str]
    va_low: Optional[str]
    va_high: Optional[str]
    method: str


class OiPoint(BaseModel):
    t: str
    value: str


class OiState(BaseModel):
    value: str
    unit: str
    at: str
    change_5m_pct: Optional[str]
    change_1h_pct: Optional[str]
    series: List[OiPoint]


class FundingState(BaseModel):
    rate_pct: str
    kind: Literal["estimated", "settled"]
    next_at: Optional[str]
    at: str


class PricePoint(BaseModel):
    type: Literal["last_trade", "mark", "index"]
    value: Optional[str]
    at: Optional[str]


class StreamQuality(BaseModel):
    stream: Literal["trades", "book", "kline", "oi", "mark", "liquidations"]
    status: Literal["ok", "stale", "book_resync", "disconnected", "incomplete", "unavailable"]
    last_at: Optional[str]
    note: Optional[str]


class DetailsResponse(BaseModel):
    instrument_id: int
    access: Access
    from_: str = Field(serialization_alias="from")
    to: str
    units: Dict[str, str]
    book: Optional[BookState]
    big_trades: BigTrades
    profile: Optional[ProfileState]
    oi: Optional[OiState]
    funding: Optional[FundingState]
    prices: List[PricePoint]
    quality: List[StreamQuality]


class Observation(BaseModel):
    kind: Literal["cvd", "oi", "wall", "bigtrade", "funding"]
    at: str
    window_s: Optional[int]
    evidence: Literal["chart", "oi", "walls"]
    values: Dict[str, str]
    units: Dict[str, str]


class ObservationsResponse(BaseModel):
    instrument_id: int
    access: Access
    at: str
    items: List[Observation]


def _access(conn, ctx: Ctx) -> MarketAccess:
    return require_market_access(resolve_access(conn, ctx.settings))


# -- endpoints ----------------------------------------------------------------------------
@router.get("/{instrument_id}/quote", response_model=QuoteResponse,
            summary="Last price, mark/index, 24 h change and a 24 h sparkline (as of the entitlement cut-off)")
def get_quote(instrument_id: int, ctx: Ctx = Depends(get_ctx)):
    with ctx.tx() as conn:
        access = _access(conn, ctx)
        inst = load_instrument(conn, instrument_id)
        body = of.quote(conn, inst, access.cutoff)
    return {"instrument_id": instrument_id, "access": access.as_dict(), **body}


@router.get("/{instrument_id}/details", response_model=DetailsResponse,
            summary="Walls, big trades, volume profile, OI, funding, prices and per-stream quality of a window")
def get_details(instrument_id: int,
                from_: Optional[AwareDatetime] = Query(None, alias="from",
                                                       description="default: 6 h before `to`"),
                to: Optional[AwareDatetime] = Query(None, description="default and maximum: the entitlement cut-off"),
                ctx: Ctx = Depends(get_ctx)):
    check_range(from_, to)
    with ctx.tx() as conn:
        access = _access(conn, ctx)
        inst = load_instrument(conn, instrument_id)
        to_eff = min(to or access.cutoff, access.cutoff)
        frm = from_ or (to_eff - DETAILS_DEFAULT_RANGE)
        if frm >= to_eff:
            raise ApiError(422, "invalid_range", "'from' must be before the data cut-off of this membership",
                           {"data_until": iso(access.cutoff)})
        if to_eff - frm > DETAILS_MAX_RANGE:
            raise ApiError(422, "range_too_large", "at most 3 days per request")
        book = of.book_state(conn, inst, to_eff, access.cutoff)
        if book is not None:
            book.pop("top", None)
        body = {
            "units": {"price": inst["quote_asset"], "size": inst["base_asset"]},
            "book": book,
            "big_trades": of.big_trades(conn, inst, frm, to_eff),
            "profile": of.profile_state(conn, inst, frm, to_eff, access.cutoff),
            "oi": of.oi_state(conn, inst, frm, to_eff),
            "funding": of.funding_state(conn, inst, frm, to_eff),
            "prices": of.prices(conn, inst, frm, to_eff),
            "quality": of.quality(conn, inst, to_eff, access.cutoff),
        }
    return {"instrument_id": instrument_id, "access": access.as_dict(), "from_": iso(frm), "to": iso(to_eff), **body}


@router.get("/{instrument_id}/observations", response_model=ObservationsResponse,
            summary="Up to five facts at `to` (not forecasts), most notable first")
def get_observations(instrument_id: int,
                     to: Optional[AwareDatetime] = Query(None, description="default and maximum: the entitlement cut-off"),
                     ctx: Ctx = Depends(get_ctx)):
    check_range(None, to)
    with ctx.tx() as conn:
        access = _access(conn, ctx)
        inst = load_instrument(conn, instrument_id)
        to_eff = min(to or access.cutoff, access.cutoff)
        items = of.observations(conn, inst, to_eff, access.cutoff)
    return {"instrument_id": instrument_id, "access": access.as_dict(), "at": iso(to_eff), "items": items}
