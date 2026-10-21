"""Personal trades entered or imported by the member (never generated from signals).

Fills and cashflows are append-only: a wrong fill is fixed by posting a correction that
names corrects_fill_id; the original stays in the audit trail and drops out of the
accounting. Trade status (draft -> open -> partially_closed -> closed, or cancelled
before any fill) is derived from the fills; data completeness is a separate field.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional, Tuple

import psycopg
from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from .. import accounting as acc
from .. import idempotency
from ..common import Ctx, DecimalIn, canonical_hash, check_range, decode_cursor, dstr, encode_cursor, get_ctx, iso
from ..errors import ApiError, not_found

router = APIRouter(prefix="/v1/trades", tags=["personal trades"])

CURRENCY = re.compile(r"^[A-Z0-9]{2,12}$")
TRADE_COLS = ("id, owner_id, instrument_id, venue_label, symbol, market_type, contract_type, contract_multiplier,"
              " quote_currency, settlement_currency, side, initial_risk_amount, initial_risk_currency,"
              " initial_risk_fx_rate, risk_locked_at, source, source_reference, status, funding_status, opened_at,"
              " closed_at, notes, created_at, updated_at")
FILL_COLS = ("id, trade_id, ts, action, quantity, price, fee, fee_currency, fee_fx_rate, fx_reference, import_key,"
             " external_id, corrects_fill_id, correction_reason, seq, created_at")
CASH_COLS = "id, trade_id, ts, kind, amount, currency, fx_rate, fx_reference, import_key, note, created_at"


def _currency(v: Optional[str], name: str) -> Optional[str]:
    if v is not None and not CURRENCY.match(v):
        raise ValueError(f"{name} must be an upper-case code such as USDT, BTC, THB")
    return v


class TradeCreate(BaseModel):
    instrument_id: Optional[int] = Field(None, description="stored instrument; its contract spec is copied")
    venue_label: Optional[str] = Field(None, max_length=60)
    symbol: Optional[str] = Field(None, max_length=40)
    market_type: Optional[Literal["spot", "perpetual", "future", "cfd", "other"]] = None
    contract_type: Optional[Literal["linear", "inverse"]] = None
    contract_multiplier: Optional[DecimalIn] = Field(
        None, description="units per lot/contract from the broker's spec (e.g. oz per CFD lot, USD per inverse "
                          "contract); required without instrument_id, never assumed")
    quote_currency: Optional[str] = None
    settlement_currency: Optional[str] = None
    side: Literal["long", "short"]
    initial_risk_amount: Optional[DecimalIn] = None
    initial_risk_currency: Optional[str] = None
    initial_risk_fx_rate: Optional[DecimalIn] = Field(None, description="risk currency -> settlement currency")
    source: Literal["manual", "import"] = "manual"
    source_reference: Optional[str] = Field(None, max_length=200)
    funding_status: Optional[Literal["not_applicable", "recorded", "unknown"]] = None
    notes: Optional[str] = Field(None, max_length=20000)

    @model_validator(mode="after")
    def _check(self):
        for n in ("quote_currency", "settlement_currency", "initial_risk_currency"):
            _currency(getattr(self, n), n)
        if (self.initial_risk_amount is None) != (self.initial_risk_currency is None):
            raise ValueError("initial_risk_amount and initial_risk_currency go together")
        if self.initial_risk_amount is not None and self.initial_risk_amount <= 0:
            raise ValueError("initial_risk_amount must be > 0")
        if self.contract_multiplier is not None and self.contract_multiplier <= 0:
            raise ValueError("contract_multiplier must be > 0")
        return self


class TradePatch(BaseModel):
    notes: Optional[str] = Field(None, max_length=20000)
    funding_status: Optional[Literal["not_applicable", "recorded", "unknown"]] = None
    status: Optional[Literal["cancelled"]] = Field(None, description="only before the first fill")
    initial_risk_amount: Optional[DecimalIn] = None
    initial_risk_currency: Optional[str] = None
    initial_risk_fx_rate: Optional[DecimalIn] = None

    @model_validator(mode="after")
    def _check(self):
        _currency(self.initial_risk_currency, "initial_risk_currency")
        if self.initial_risk_amount is not None and self.initial_risk_amount <= 0:
            raise ValueError("initial_risk_amount must be > 0")
        return self


class FillCreate(BaseModel):
    ts: AwareDatetime
    action: Literal["entry", "exit"]
    quantity: DecimalIn
    price: DecimalIn
    fee: Optional[DecimalIn] = Field(None, description="paid (+) or rebate (-); omit when unknown (trade incomplete)")
    fee_currency: Optional[str] = None
    fee_fx_rate: Optional[DecimalIn] = Field(None, description="fee currency -> settlement currency")
    fx_reference: Optional[str] = Field(None, max_length=200)
    import_key: Optional[str] = Field(None, min_length=1, max_length=200,
                                      description="unique per member; re-importing the same key is a no-op")
    external_id: Optional[str] = Field(None, max_length=200)
    corrects_fill_id: Optional[uuid.UUID] = None
    correction_reason: Optional[str] = Field(None, max_length=2000)

    @model_validator(mode="after")
    def _check(self):
        if self.quantity <= 0 or self.price <= 0:
            raise ValueError("quantity and price must be > 0")
        if self.fee is not None and self.fee_currency is None:
            raise ValueError("fee_currency is required with fee")
        _currency(self.fee_currency, "fee_currency")
        if (self.corrects_fill_id is None) != (not self.correction_reason):
            raise ValueError("corrects_fill_id and correction_reason go together")
        if self.fee_fx_rate is not None and self.fee_fx_rate <= 0:
            raise ValueError("fee_fx_rate must be > 0")
        return self


class CashflowCreate(BaseModel):
    ts: AwareDatetime
    kind: Literal["funding", "rebate", "fee", "adjustment"]
    amount: DecimalIn = Field(description="signed: + credited to the member, - debited")
    currency: str
    fx_rate: Optional[DecimalIn] = Field(None, description="currency -> settlement currency")
    fx_reference: Optional[str] = Field(None, max_length=200)
    import_key: Optional[str] = Field(None, min_length=1, max_length=200)
    note: Optional[str] = Field(None, max_length=2000)

    @model_validator(mode="after")
    def _check(self):
        _currency(self.currency, "currency")
        if self.fx_rate is not None and self.fx_rate <= 0:
            raise ValueError("fx_rate must be > 0")
        return self


class FillOut(BaseModel):
    id: uuid.UUID
    trade_id: uuid.UUID
    ts: str
    action: str
    quantity: str
    price: str
    fee: Optional[str]
    fee_currency: Optional[str]
    fee_fx_rate: Optional[str]
    fx_reference: Optional[str]
    import_key: Optional[str]
    external_id: Optional[str]
    corrects_fill_id: Optional[uuid.UUID]
    correction_reason: Optional[str]
    superseded: bool
    created_at: str


class CashflowOut(BaseModel):
    id: uuid.UUID
    trade_id: uuid.UUID
    ts: str
    kind: str
    amount: str
    currency: str
    fx_rate: Optional[str]
    fx_reference: Optional[str]
    import_key: Optional[str]
    note: Optional[str]
    created_at: str


class TradeSummary(BaseModel):
    status: str
    settlement_currency: str
    open_quantity: Optional[str]
    entry_quantity: Optional[str]
    exit_quantity: Optional[str]
    avg_entry_price: Optional[str]
    avg_exit_price: Optional[str]
    gross_pnl: Optional[str]
    fees: Optional[str]
    funding: Optional[str]
    other_cashflows: Optional[str]
    net_pnl: Optional[str]
    net_r: Optional[str] = Field(description="null = N/A (no initial risk or no FX rate for it)")
    r_unavailable_reason: Optional[str]
    complete: bool
    incomplete_reasons: List[str]
    opened_at: Optional[str]
    closed_at: Optional[str]


class TradeOut(BaseModel):
    id: uuid.UUID
    instrument_id: Optional[int]
    venue_label: str
    symbol: str
    market_type: str
    contract_type: str
    contract_multiplier: str
    quote_currency: str
    settlement_currency: str
    side: str
    initial_risk_amount: Optional[str]
    initial_risk_currency: Optional[str]
    initial_risk_fx_rate: Optional[str]
    risk_locked_at: Optional[str]
    source: str
    source_reference: Optional[str]
    status: str
    funding_status: str
    opened_at: Optional[str]
    closed_at: Optional[str]
    notes: Optional[str]
    created_at: str
    updated_at: str
    summary: Optional[TradeSummary]
    fills: Optional[List[FillOut]] = None
    cashflows: Optional[List[CashflowOut]] = None


class TradePage(BaseModel):
    items: List[TradeOut]
    next_cursor: Optional[str]


class FillResult(BaseModel):
    fill: FillOut
    trade: TradeOut


class CashflowResult(BaseModel):
    cashflow: CashflowOut
    trade: TradeOut


# -- helpers (also used by export) ---------------------------------------------------------
def fill_dc(r: dict) -> acc.Fill:
    return acc.Fill(id=str(r["id"]), ts=r["ts"], action=r["action"], quantity=r["quantity"], price=r["price"],
                    fee=r["fee"], fee_currency=r["fee_currency"], fee_fx_rate=r["fee_fx_rate"], seq=r["seq"],
                    corrects_fill_id=str(r["corrects_fill_id"]) if r["corrects_fill_id"] else None)


def cash_dc(r: dict) -> acc.Cashflow:
    return acc.Cashflow(id=str(r["id"]), ts=r["ts"], kind=r["kind"], amount=r["amount"], currency=r["currency"],
                        fx_rate=r["fx_rate"])


def compute(trade: dict, fills: List[dict], cashflows: List[dict]) -> acc.TradeResult:
    contract = acc.Contract(trade["side"], trade["contract_type"], trade["contract_multiplier"],
                            trade["settlement_currency"])
    risk = None
    if trade["initial_risk_amount"] is not None:
        risk = acc.InitialRisk(trade["initial_risk_amount"], trade["initial_risk_currency"],
                               trade["initial_risk_fx_rate"])
    return acc.compute_trade(contract, [fill_dc(f) for f in fills], [cash_dc(c) for c in cashflows], risk,
                             trade["funding_status"], cancelled=trade["status"] == "cancelled")


def fill_out(r: dict, superseded: bool = False) -> dict:
    d = {k: r[k] for k in ("id", "trade_id", "action", "fee_currency", "fx_reference", "import_key", "external_id",
                           "corrects_fill_id", "correction_reason")}
    d.update(ts=iso(r["ts"]), created_at=iso(r["created_at"]), quantity=dstr(r["quantity"]), price=dstr(r["price"]),
             fee=dstr(r["fee"]), fee_fx_rate=dstr(r["fee_fx_rate"]), superseded=superseded)
    return d


def cash_out(r: dict) -> dict:
    d = {k: r[k] for k in ("id", "trade_id", "kind", "currency", "fx_reference", "import_key", "note")}
    d.update(ts=iso(r["ts"]), created_at=iso(r["created_at"]), amount=dstr(r["amount"]), fx_rate=dstr(r["fx_rate"]))
    return d


def trade_out(t: dict, fills: List[dict], cashflows: List[dict], detail: bool = False) -> dict:
    d = {k: t[k] for k in ("id", "instrument_id", "venue_label", "symbol", "market_type", "contract_type",
                           "quote_currency", "settlement_currency", "side", "initial_risk_currency", "source",
                           "source_reference", "status", "funding_status", "notes")}
    d.update(contract_multiplier=dstr(t["contract_multiplier"]), initial_risk_amount=dstr(t["initial_risk_amount"]),
             initial_risk_fx_rate=dstr(t["initial_risk_fx_rate"]), risk_locked_at=iso(t["risk_locked_at"]),
             opened_at=iso(t["opened_at"]), closed_at=iso(t["closed_at"]), created_at=iso(t["created_at"]),
             updated_at=iso(t["updated_at"]))
    try:
        d["summary"] = compute(t, fills, cashflows).as_dict()
    except acc.FillSequenceError:
        d["summary"] = None
    if detail:
        replaced = {f["corrects_fill_id"] for f in fills if f["corrects_fill_id"]}
        d["fills"] = [fill_out(f, f["id"] in replaced) for f in fills]
        d["cashflows"] = [cash_out(c) for c in cashflows]
    return d


def load_children(conn: psycopg.Connection, owner: uuid.UUID, trade_ids: List[uuid.UUID]
                  ) -> Tuple[Dict[uuid.UUID, List[dict]], Dict[uuid.UUID, List[dict]]]:
    fills: Dict[uuid.UUID, List[dict]] = {t: [] for t in trade_ids}
    cash: Dict[uuid.UUID, List[dict]] = {t: [] for t in trade_ids}
    if not trade_ids:
        return fills, cash
    for r in conn.execute(f"SELECT {FILL_COLS} FROM fills WHERE owner_id = %s AND trade_id = ANY(%s)"
                          " ORDER BY ts, seq", [owner, trade_ids]):
        fills[r["trade_id"]].append(r)
    for r in conn.execute(f"SELECT {CASH_COLS} FROM cashflows WHERE owner_id = %s AND trade_id = ANY(%s)"
                          " ORDER BY ts, id", [owner, trade_ids]):
        cash[r["trade_id"]].append(r)
    return fills, cash


def _load(conn: psycopg.Connection, owner: uuid.UUID, trade_id: uuid.UUID, lock: bool = False
          ) -> Tuple[dict, List[dict], List[dict]]:
    t = conn.execute(f"SELECT {TRADE_COLS} FROM personal_trades WHERE id = %s AND owner_id = %s"
                     + (" FOR UPDATE" if lock else ""), [trade_id, owner]).fetchone()
    if t is None:
        raise not_found("trade")
    fills, cash = load_children(conn, owner, [trade_id])
    return t, fills[trade_id], cash[trade_id]


def _refresh_status(conn: psycopg.Connection, owner: uuid.UUID, trade: dict, fills: List[dict],
                    cash: List[dict]) -> dict:
    try:
        res = compute(trade, fills, cash)
    except acc.FillSequenceError as e:
        raise ApiError(422, "invalid_fill_sequence", str(e)) from None
    return conn.execute(
        f"UPDATE personal_trades SET status = %s, opened_at = %s, closed_at = %s,"
        f" risk_locked_at = CASE WHEN %s THEN coalesce(risk_locked_at, now()) ELSE risk_locked_at END,"
        f" updated_at = now() WHERE id = %s AND owner_id = %s RETURNING {TRADE_COLS}",
        [res.status, res.opened_at, res.closed_at, bool(fills), trade["id"], owner]).fetchone()


# -- endpoints ----------------------------------------------------------------------------
@router.get("/stats", summary="Statistics over every closed trade in the range (computed server-side)")
def trade_stats(from_: Optional[AwareDatetime] = Query(None, alias="from", description="closed_at >= from"),
                to: Optional[AwareDatetime] = Query(None, description="closed_at < to"),
                ctx: Ctx = Depends(get_ctx)) -> Dict[str, Any]:
    check_range(from_, to)
    with ctx.tx() as conn:
        trades = conn.execute(
            f"SELECT {TRADE_COLS} FROM personal_trades WHERE owner_id = %(o)s AND status = 'closed'"
            " AND (%(f)s::timestamptz IS NULL OR closed_at >= %(f)s) AND (%(t)s::timestamptz IS NULL OR closed_at < %(t)s)"
            " ORDER BY closed_at, id", {"o": ctx.user_id, "f": from_, "t": to}).fetchall()
        fills, cash = load_children(conn, ctx.user_id, [t["id"] for t in trades])
        not_closed = conn.execute(
            "SELECT count(*) AS n FROM personal_trades WHERE owner_id = %(o)s AND status IN ('open', 'partially_closed')"
            " AND (%(t)s::timestamptz IS NULL OR opened_at < %(t)s)", {"o": ctx.user_id, "t": to}).fetchone()["n"]
    closed = []
    for t in trades:
        try:
            closed.append(acc.ClosedTrade(str(t["id"]), t["closed_at"], compute(t, fills[t["id"]], cash[t["id"]])))
        except acc.FillSequenceError:
            continue
    stats = acc.compute_stats(closed)
    stats["range"] = {"from": iso(from_), "to": iso(to), "basis": "closed_at"}
    stats["open_positions_excluded"] = not_closed
    stats["definitions"] = acc.DEFINITIONS
    stats["source"] = "member-entered and imported trades only; no simulated or research results"
    return stats


@router.post("", status_code=201, response_model=TradeOut, summary="Create a personal trade (draft until a fill)")
def create_trade(body: TradeCreate, ctx: Ctx = Depends(get_ctx),
                 idempotency_key: Optional[str] = Header(None, description=idempotency.HEADER_DESCRIPTION)):
    key = idempotency.check_key(idempotency_key, required=False)
    v = body.model_dump()
    with ctx.tx() as conn:
        if key:
            existing = idempotency.claim(conn, ctx.user_id, "trade.create", key, canonical_hash(v))
            if existing is not None:
                t, f, c = _load(conn, ctx.user_id, existing)
                return JSONResponse(TradeOut(**trade_out(t, f, c, True)).model_dump(mode="json"), status_code=200,
                                    headers={"Idempotent-Replayed": "true"})
        spec_fields = ("venue_label", "symbol", "market_type", "contract_type", "contract_multiplier",
                       "quote_currency", "settlement_currency")
        if v["instrument_id"] is not None:
            inst = conn.execute("SELECT * FROM instruments WHERE instrument_id = %s", [v["instrument_id"]]).fetchone()
            if inst is None:
                raise ApiError(422, "instrument_not_found", "instrument_id does not exist")
            spec = {"venue_label": inst["venue"], "symbol": inst["symbol"],
                    "market_type": inst["market_type"] if inst["market_type"] != "index" else "other",
                    "contract_type": "linear" if inst["contract_type"] == "spot" else inst["contract_type"],
                    "contract_multiplier": inst["contract_multiplier"], "quote_currency": inst["quote_asset"],
                    "settlement_currency": inst["settlement_asset"]}
            clash = [f for f in spec_fields if v[f] is not None and v[f] != spec[f]]
            if clash:
                raise ApiError(422, "spec_mismatch", "fields differ from the stored instrument", {"fields": clash})
            v.update(spec)
        else:
            missing = [f for f in spec_fields if v[f] is None]
            if missing:
                raise ApiError(422, "contract_spec_required",
                               "without instrument_id the contract specification is required (no defaults are assumed)",
                               {"missing": missing})
        if v["funding_status"] is None:
            v["funding_status"] = "not_applicable" if v["market_type"] == "spot" else "unknown"
        tid = uuid.uuid4()
        cols = ["id", "owner_id", "instrument_id", *spec_fields, "side", "initial_risk_amount", "initial_risk_currency",
                "initial_risk_fx_rate", "source", "source_reference", "funding_status", "notes"]
        vals = [tid, ctx.user_id] + [v[c] for c in cols[2:]]
        t = conn.execute(f"INSERT INTO personal_trades ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
                         f" RETURNING {TRADE_COLS}", vals).fetchone()
        if key:
            idempotency.bind(conn, ctx.user_id, "trade.create", key, tid)
    return trade_out(t, [], [], True)


@router.get("", response_model=TradePage, summary="List own trades, newest first")
def list_trades(status: Optional[Literal["draft", "open", "partially_closed", "closed", "cancelled"]] = None,
                from_: Optional[AwareDatetime] = Query(None, alias="from", description="created_at >= from"),
                to: Optional[AwareDatetime] = Query(None, description="created_at < to"),
                cursor: Optional[str] = None,
                limit: int = Query(50, ge=1, le=100),
                ctx: Ctx = Depends(get_ctx)):
    check_range(from_, to)
    c_ts, c_id = decode_cursor(cursor) if cursor else (None, None)
    with ctx.tx() as conn:
        rows = conn.execute(
            f"SELECT {TRADE_COLS} FROM personal_trades WHERE owner_id = %(o)s"
            " AND (%(s)s::text IS NULL OR status = %(s)s)"
            " AND (%(f)s::timestamptz IS NULL OR created_at >= %(f)s) AND (%(t)s::timestamptz IS NULL OR created_at < %(t)s)"
            " AND (%(cts)s::timestamptz IS NULL OR (created_at, id) < (%(cts)s, %(cid)s::uuid))"
            " ORDER BY created_at DESC, id DESC LIMIT %(lim)s",
            {"o": ctx.user_id, "s": status, "f": from_, "t": to, "cts": c_ts, "cid": c_id, "lim": limit + 1}).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        fills, cash = load_children(conn, ctx.user_id, [r["id"] for r in rows])
    nxt = encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if more and rows else None
    return {"items": [trade_out(r, fills[r["id"]], cash[r["id"]]) for r in rows], "next_cursor": nxt}


@router.get("/{trade_id}", response_model=TradeOut, summary="One own trade with fills, cashflows and its summary")
def get_trade(trade_id: uuid.UUID, ctx: Ctx = Depends(get_ctx)):
    with ctx.tx() as conn:
        t, f, c = _load(conn, ctx.user_id, trade_id)
    return trade_out(t, f, c, True)


@router.patch("/{trade_id}", response_model=TradeOut,
              summary="Edit notes/funding status, cancel a draft, or set initial risk before the first fill")
def patch_trade(trade_id: uuid.UUID, body: TradePatch, ctx: Ctx = Depends(get_ctx)):
    provided = body.model_fields_set
    with ctx.tx() as conn:
        t, f, c = _load(conn, ctx.user_id, trade_id, lock=True)
        sets: Dict[str, Any] = {}
        if "status" in provided and body.status == "cancelled":
            if f:
                raise ApiError(409, "trade_has_fills", "a trade with fills cannot be cancelled; close it with fills")
            sets["status"] = "cancelled"
        risk_fields = {"initial_risk_amount", "initial_risk_currency", "initial_risk_fx_rate"} & provided
        if risk_fields:
            if t["risk_locked_at"] is not None:
                raise ApiError(409, "initial_risk_locked", "initial risk is locked once the first fill exists")
            for k in risk_fields:
                sets[k] = getattr(body, k)
            amount = sets.get("initial_risk_amount", t["initial_risk_amount"])
            currency = sets.get("initial_risk_currency", t["initial_risk_currency"])
            if (amount is None) != (currency is None):
                raise ApiError(422, "validation_error", "initial_risk_amount and initial_risk_currency go together")
        for k in ("notes", "funding_status"):
            if k in provided:
                if k == "funding_status" and body.funding_status is None:
                    raise ApiError(422, "validation_error", "funding_status cannot be null")
                sets[k] = getattr(body, k)
        if sets:
            assign = ", ".join(f"{k} = %s" for k in sets)
            t = conn.execute(f"UPDATE personal_trades SET {assign}, updated_at = now() WHERE id = %s AND owner_id = %s"
                             f" RETURNING {TRADE_COLS}", [*sets.values(), trade_id, ctx.user_id]).fetchone()
    return trade_out(t, f, c, True)


@router.delete("/{trade_id}", status_code=204, summary="Delete a trade with its fills and cashflows permanently")
def delete_trade(trade_id: uuid.UUID, ctx: Ctx = Depends(get_ctx)):
    with ctx.tx() as conn:
        row = conn.execute("DELETE FROM personal_trades WHERE id = %s AND owner_id = %s RETURNING id",
                           [trade_id, ctx.user_id]).fetchone()
        if row is None:
            raise not_found("trade")
        conn.execute("INSERT INTO deletion_tombstones (owner_id, entity, entity_id) VALUES (%s, 'personal_trade', %s)"
                     " ON CONFLICT DO NOTHING", [ctx.user_id, trade_id])
    return Response(status_code=204)


def _same_fill(r: dict, body: FillCreate, trade_id: uuid.UUID) -> bool:
    return (r["trade_id"] == trade_id and r["ts"] == body.ts and r["action"] == body.action
            and r["quantity"] == body.quantity and r["price"] == body.price and r["fee"] == body.fee
            and r["fee_currency"] == body.fee_currency and r["fee_fx_rate"] == body.fee_fx_rate
            and r["corrects_fill_id"] == body.corrects_fill_id)


@router.post("/{trade_id}/fills", status_code=201, response_model=FillResult,
             responses={200: {"model": FillResult, "description": "import_key already imported with the same values"},
                        409: {"description": "import_key reused with different values, or fill already corrected"},
                        422: {"description": "the fill would make the position invalid"}},
             summary="Add a fill or a correction of an earlier fill")
def add_fill(trade_id: uuid.UUID, body: FillCreate, ctx: Ctx = Depends(get_ctx)):
    with ctx.tx() as conn:
        t, fills, cash = _load(conn, ctx.user_id, trade_id, lock=True)
        if t["status"] == "cancelled":
            raise ApiError(409, "trade_cancelled", "the trade was cancelled")
        if body.import_key is not None:
            prev = conn.execute(f"SELECT {FILL_COLS} FROM fills WHERE owner_id = %s AND import_key = %s",
                                [ctx.user_id, body.import_key]).fetchone()
            if prev is not None:
                if not _same_fill(prev, body, trade_id):
                    raise ApiError(409, "import_key_conflict", "this import_key was imported with different values")
                return JSONResponse(FillResult(fill=fill_out(prev), trade=trade_out(t, fills, cash, True))
                                    .model_dump(mode="json"), status_code=200, headers={"Idempotent-Replayed": "true"})
        if body.corrects_fill_id is not None:
            target = next((f for f in fills if f["id"] == body.corrects_fill_id), None)
            if target is None:
                raise ApiError(422, "fill_not_found", "corrects_fill_id is not a fill of this trade")
            if any(f["corrects_fill_id"] == body.corrects_fill_id for f in fills):
                raise ApiError(409, "already_corrected", "that fill was already corrected; correct the correction")
        row = conn.execute(
            f"INSERT INTO fills (id, owner_id, trade_id, ts, action, quantity, price, fee, fee_currency, fee_fx_rate,"
            f" fx_reference, import_key, external_id, corrects_fill_id, correction_reason)"
            f" VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {FILL_COLS}",
            [uuid.uuid4(), ctx.user_id, trade_id, body.ts, body.action, body.quantity, body.price, body.fee,
             body.fee_currency, body.fee_fx_rate, body.fx_reference, body.import_key, body.external_id,
             body.corrects_fill_id, body.correction_reason]).fetchone()
        fills = sorted(fills + [row], key=lambda f: (f["ts"], f["seq"]))
        t = _refresh_status(conn, ctx.user_id, t, fills, cash)
    return {"fill": fill_out(row), "trade": trade_out(t, fills, cash, True)}


@router.post("/{trade_id}/cashflows", status_code=201, response_model=CashflowResult,
             responses={200: {"model": CashflowResult, "description": "import_key already imported"}},
             summary="Add funding, a rebate, an extra fee or an adjustment")
def add_cashflow(trade_id: uuid.UUID, body: CashflowCreate, ctx: Ctx = Depends(get_ctx)):
    with ctx.tx() as conn:
        t, fills, cash = _load(conn, ctx.user_id, trade_id, lock=True)
        if body.import_key is not None:
            prev = conn.execute(f"SELECT {CASH_COLS} FROM cashflows WHERE owner_id = %s AND import_key = %s",
                                [ctx.user_id, body.import_key]).fetchone()
            if prev is not None:
                same = (prev["trade_id"] == trade_id and prev["ts"] == body.ts and prev["kind"] == body.kind
                        and prev["amount"] == body.amount and prev["currency"] == body.currency
                        and prev["fx_rate"] == body.fx_rate)
                if not same:
                    raise ApiError(409, "import_key_conflict", "this import_key was imported with different values")
                return JSONResponse(CashflowResult(cashflow=cash_out(prev), trade=trade_out(t, fills, cash, True))
                                    .model_dump(mode="json"), status_code=200, headers={"Idempotent-Replayed": "true"})
        row = conn.execute(
            f"INSERT INTO cashflows (id, owner_id, trade_id, ts, kind, amount, currency, fx_rate, fx_reference,"
            f" import_key, note) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {CASH_COLS}",
            [uuid.uuid4(), ctx.user_id, trade_id, body.ts, body.kind, body.amount, body.currency, body.fx_rate,
             body.fx_reference, body.import_key, body.note]).fetchone()
        cash = cash + [row]
        t = conn.execute(f"UPDATE personal_trades SET updated_at = now() WHERE id = %s AND owner_id = %s"
                         f" RETURNING {TRADE_COLS}", [trade_id, ctx.user_id]).fetchone()
    return {"cashflow": cash_out(row), "trade": trade_out(t, fills, cash, True)}
