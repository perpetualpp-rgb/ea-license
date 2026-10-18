"""Personal-trade accounting with Decimal end to end (no floats).

Definitions (also returned by GET /v1/trades/stats):

* Realized PnL uses the average-cost method on effective fills (a fill replaced by a
  correction is ignored), in fill time order:
    linear  long : (exit - avg_entry) * qty * multiplier            in the quote/settlement currency
    linear  short: (avg_entry - exit) * qty * multiplier
    inverse long : qty * multiplier * (1/avg_entry - 1/exit)        in the base/settlement coin
    inverse short: qty * multiplier * (1/exit - 1/avg_entry)
  where inverse avg_entry is the contract-weighted harmonic mean (cost is tracked as
  coin value qty*multiplier/price). Spot is linear with multiplier 1.
* Fees (paid +, rebate -) and cashflows are converted to the settlement currency with the
  rate stored on the row; a missing rate or an unknown fee makes the trade incomplete.
* Net PnL = gross realized PnL - fees + funding + other cashflows (signed, + = credited).
* Net R = net PnL / initial risk locked before the first fill (converted with its stored
  rate). No initial risk -> N/A (None), never guessed.
* A trade is complete when it is closed, every fee and cashflow is known and convertible,
  and funding is recorded or not applicable.
* Win rate = complete closed trades with net PnL > 0 / complete closed trades. Breakeven
  (net PnL == 0 at the reporting precision) and incomplete trades are counted separately.
* Expectancy = mean net PnL per complete closed trade (per currency) and mean net R.
* Profit factor = sum of winning net PnL / |sum of losing net PnL| (None without losses).
* Realized-equity drawdown = largest fall of cumulative realized net PnL (complete closed
  trades ordered by close time, starting from 0). It is NOT a mark-to-market drawdown:
  open-position swings are not included.

Full precision is kept while summing; amounts are rounded once, at the end, to 8 decimal
places (ROUND_HALF_EVEN) and R/ratios to 4.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

AMOUNT_PLACES = Decimal("0.00000001")
RATIO_PLACES = Decimal("0.0001")
PRECISION = 60
ZERO = Decimal(0)


class FillSequenceError(ValueError):
    """Fills do not form a valid position (exit before entry, exit larger than position)."""


@dataclass(frozen=True)
class Contract:
    side: str                  # 'long' | 'short'
    contract_type: str         # 'linear' | 'inverse'
    multiplier: Decimal
    settlement_currency: str


@dataclass(frozen=True)
class Fill:
    id: str
    ts: datetime
    action: str                # 'entry' | 'exit'
    quantity: Decimal
    price: Decimal
    fee: Optional[Decimal] = None
    fee_currency: Optional[str] = None
    fee_fx_rate: Optional[Decimal] = None
    seq: int = 0
    corrects_fill_id: Optional[str] = None


@dataclass(frozen=True)
class Cashflow:
    id: str
    ts: datetime
    kind: str                  # 'funding' | 'rebate' | 'fee' | 'adjustment'
    amount: Decimal            # + credited, - debited
    currency: str
    fx_rate: Optional[Decimal] = None


@dataclass(frozen=True)
class InitialRisk:
    amount: Decimal
    currency: str
    fx_rate: Optional[Decimal] = None


@dataclass
class TradeResult:
    status: str
    settlement_currency: str
    open_quantity: Decimal = ZERO
    entry_quantity: Decimal = ZERO
    exit_quantity: Decimal = ZERO
    avg_entry_price: Optional[Decimal] = None
    avg_exit_price: Optional[Decimal] = None
    gross_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    funding: Decimal = ZERO
    other_cashflows: Decimal = ZERO
    net_pnl: Decimal = ZERO
    net_r: Optional[Decimal] = None
    r_unavailable_reason: Optional[str] = None
    complete: bool = False
    incomplete_reasons: List[str] = field(default_factory=list)
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    exits: int = 0

    @property
    def net_pnl_rounded(self) -> Decimal:
        return q_amount(self.net_pnl)

    def as_dict(self) -> dict:
        from .common import dstr, iso
        return {
            "status": self.status, "settlement_currency": self.settlement_currency,
            "open_quantity": dstr(self.open_quantity), "entry_quantity": dstr(self.entry_quantity),
            "exit_quantity": dstr(self.exit_quantity),
            "avg_entry_price": dstr(q_price(self.avg_entry_price)), "avg_exit_price": dstr(q_price(self.avg_exit_price)),
            "gross_pnl": dstr(q_amount(self.gross_pnl)), "fees": dstr(q_amount(self.fees)),
            "funding": dstr(q_amount(self.funding)), "other_cashflows": dstr(q_amount(self.other_cashflows)),
            "net_pnl": dstr(q_amount(self.net_pnl)),
            "net_r": dstr(q_ratio(self.net_r)), "r_unavailable_reason": self.r_unavailable_reason,
            "complete": self.complete, "incomplete_reasons": list(self.incomplete_reasons),
            "opened_at": iso(self.opened_at), "closed_at": iso(self.closed_at),
        }


def q_amount(d: Optional[Decimal]) -> Optional[Decimal]:
    if d is None:
        return None
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return d.quantize(AMOUNT_PLACES, rounding=ROUND_HALF_EVEN)


def q_ratio(d: Optional[Decimal]) -> Optional[Decimal]:
    if d is None:
        return None
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return d.quantize(RATIO_PLACES, rounding=ROUND_HALF_EVEN)


def q_price(d: Optional[Decimal]) -> Optional[Decimal]:
    if d is None:
        return None
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return d.quantize(Decimal("0.0000000001"), rounding=ROUND_HALF_EVEN)


def effective_fills(fills: Iterable[Fill]) -> List[Fill]:
    """Drop fills that a later correction replaced (chains resolve to the last correction)."""
    fills = list(fills)
    replaced = {f.corrects_fill_id for f in fills if f.corrects_fill_id}
    return sorted((f for f in fills if f.id not in replaced), key=lambda f: (f.ts, f.seq))


def _to_settlement(amount: Decimal, currency: Optional[str], rate: Optional[Decimal], settlement: str) -> Optional[Decimal]:
    if currency == settlement:
        return amount
    if rate is not None:
        return amount * rate
    return None


def compute_trade(contract: Contract, fills: Sequence[Fill], cashflows: Sequence[Cashflow] = (),
                  initial_risk: Optional[InitialRisk] = None, funding_status: str = "not_applicable",
                  cancelled: bool = False) -> TradeResult:
    if contract.side not in ("long", "short") or contract.contract_type not in ("linear", "inverse"):
        raise ValueError("unsupported contract")
    res = TradeResult(status="cancelled" if cancelled else "draft", settlement_currency=contract.settlement_currency)
    reasons: List[str] = []
    with localcontext() as ctx:
        ctx.prec = PRECISION
        ctx.rounding = ROUND_HALF_EVEN
        mult = contract.multiplier
        linear = contract.contract_type == "linear"
        long = contract.side == "long"
        pos = ZERO
        cost = ZERO            # linear: sum(qty*price*mult) of the open position; inverse: sum(qty*mult/price)
        entry_value = ZERO     # same measure over all entries (for the average entry price)
        exit_value = ZERO
        gross = ZERO
        eff = effective_fills(fills)
        for f in eff:
            value = f.quantity * f.price * mult if linear else f.quantity * mult / f.price
            if f.action == "entry":
                if res.opened_at is None:
                    res.opened_at = f.ts
                pos += f.quantity
                cost += value
                entry_value += value
                res.entry_quantity += f.quantity
            elif f.action == "exit":
                if pos == 0:
                    raise FillSequenceError(f"fill {f.id}: exit without an open position")
                if f.quantity > pos:
                    raise FillSequenceError(f"fill {f.id}: exit {f.quantity} exceeds open quantity {pos}")
                portion = cost if f.quantity == pos else cost * f.quantity / pos
                if linear:
                    pnl = (value - portion) if long else (portion - value)
                else:
                    pnl = (portion - value) if long else (value - portion)
                gross += pnl
                cost -= portion
                pos -= f.quantity
                if pos == 0:
                    cost = ZERO
                exit_value += value
                res.exit_quantity += f.quantity
                res.exits += 1
                if pos == 0:
                    res.closed_at = f.ts
            else:
                raise FillSequenceError(f"fill {f.id}: unknown action {f.action!r}")
            if f.fee is None:
                reasons.append(f"fee_unknown:{f.id}")
            else:
                fee = _to_settlement(f.fee, f.fee_currency, f.fee_fx_rate, contract.settlement_currency)
                if fee is None:
                    reasons.append(f"fee_fx_missing:{f.id}")
                else:
                    res.fees += fee
        if pos != 0:
            res.closed_at = None

        for c in cashflows:
            amount = _to_settlement(c.amount, c.currency, c.fx_rate, contract.settlement_currency)
            if amount is None:
                reasons.append(f"cashflow_fx_missing:{c.id}")
                continue
            if c.kind == "funding":
                res.funding += amount
            else:
                res.other_cashflows += amount

        res.open_quantity = pos
        res.gross_pnl = gross
        res.net_pnl = gross - res.fees + res.funding + res.other_cashflows
        if res.entry_quantity:
            res.avg_entry_price = (entry_value / (res.entry_quantity * mult) if linear
                                   else res.entry_quantity * mult / entry_value)
        if res.exit_quantity:
            res.avg_exit_price = (exit_value / (res.exit_quantity * mult) if linear
                                  else res.exit_quantity * mult / exit_value)

        if not cancelled and eff:
            if pos == 0:
                res.status = "closed"
            elif res.exits:
                res.status = "partially_closed"
            else:
                res.status = "open"

        if res.status != "closed":
            reasons.append("not_closed")
        if funding_status == "unknown":
            reasons.append("funding_unknown")

        if initial_risk is None:
            res.r_unavailable_reason = "no_initial_risk"
        else:
            risk = _to_settlement(initial_risk.amount, initial_risk.currency, initial_risk.fx_rate,
                                  contract.settlement_currency)
            if risk is None:
                res.r_unavailable_reason = "initial_risk_fx_missing"
            elif risk <= 0:
                res.r_unavailable_reason = "initial_risk_not_positive"
            else:
                res.net_r = res.net_pnl / risk
    res.incomplete_reasons = reasons
    res.complete = not reasons
    return res


# -- statistics ------------------------------------------------------------------------
@dataclass(frozen=True)
class ClosedTrade:
    id: str
    closed_at: datetime
    result: TradeResult


def _median(values: List[Decimal]) -> Optional[Decimal]:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def realized_drawdown(points: List[Tuple[datetime, Decimal]]) -> dict:
    """Largest fall of cumulative realized net PnL from its running peak (start = 0)."""
    from .common import dstr, iso
    equity = peak = ZERO
    peak_at = None
    best = ZERO
    best_peak_at = best_trough_at = None
    for ts, pnl in points:
        equity += pnl
        if equity > peak:
            peak, peak_at = equity, ts
        dd = peak - equity
        if dd > best:
            best, best_peak_at, best_trough_at = dd, peak_at, ts
    return {"amount": dstr(q_amount(best)), "peak_at": iso(best_peak_at), "trough_at": iso(best_trough_at),
            "final_equity": dstr(q_amount(equity)), "basis": "realized net PnL of complete closed trades, start 0"}


def compute_stats(trades: Sequence[ClosedTrade]) -> dict:
    from .common import dstr
    with localcontext() as ctx:
        ctx.prec = PRECISION
        ctx.rounding = ROUND_HALF_EVEN
        closed = [t for t in trades if t.result.status == "closed"]
        complete = [t for t in closed if t.result.complete]
        incomplete = [t for t in closed if not t.result.complete]
        wins = [t for t in complete if t.result.net_pnl_rounded > 0]
        losses = [t for t in complete if t.result.net_pnl_rounded < 0]
        breakeven = [t for t in complete if t.result.net_pnl_rounded == 0]
        r_values = [t.result.net_r for t in complete if t.result.net_r is not None]

        by_currency: Dict[str, dict] = {}
        for cur in sorted({t.result.settlement_currency for t in complete}):
            group = sorted((t for t in complete if t.result.settlement_currency == cur),
                           key=lambda t: (t.closed_at, t.id))
            nets = [t.result.net_pnl for t in group]
            profit = sum((n for n in nets if q_amount(n) > 0), ZERO)
            loss = sum((n for n in nets if q_amount(n) < 0), ZERO)
            by_currency[cur] = {
                "sample": len(group),
                "gross_pnl": dstr(q_amount(sum((t.result.gross_pnl for t in group), ZERO))),
                "fees": dstr(q_amount(sum((t.result.fees for t in group), ZERO))),
                "funding": dstr(q_amount(sum((t.result.funding for t in group), ZERO))),
                "other_cashflows": dstr(q_amount(sum((t.result.other_cashflows for t in group), ZERO))),
                "net_pnl": dstr(q_amount(sum(nets, ZERO))),
                "gross_profit": dstr(q_amount(profit)),
                "gross_loss": dstr(q_amount(loss)),
                "expectancy": dstr(q_amount(sum(nets, ZERO) / len(group))),
                "mean_net_pnl": dstr(q_amount(sum(nets, ZERO) / len(group))),
                "median_net_pnl": dstr(q_amount(_median(nets))),
                "profit_factor": dstr(q_ratio(profit / -loss)) if loss < 0 else None,
                "realized_equity_max_drawdown": realized_drawdown([(t.closed_at, t.result.net_pnl) for t in group]),
            }

        return {
            "counts": {
                "closed": len(closed), "complete": len(complete), "incomplete": len(incomplete),
                "wins": len(wins), "losses": len(losses), "breakeven": len(breakeven),
                "without_initial_risk": sum(1 for t in complete if t.result.net_r is None),
            },
            "win_rate": dstr(q_ratio(Decimal(len(wins)) / len(complete))) if complete else None,
            "r": {
                "sample": len(r_values),
                "mean": dstr(q_ratio(sum(r_values, ZERO) / len(r_values))) if r_values else None,
                "median": dstr(q_ratio(_median(r_values))) if r_values else None,
                "expectancy_r": dstr(q_ratio(sum(r_values, ZERO) / len(r_values))) if r_values else None,
                "total": dstr(q_ratio(sum(r_values, ZERO))) if r_values else None,
            },
            "by_currency": by_currency,
            "incomplete_trades": [{"id": t.id, "reasons": t.result.incomplete_reasons} for t in incomplete],
        }


DEFINITIONS = {
    "range_basis": "closed trades whose closed_at is in [from, to); open trades are excluded",
    "realized_pnl": "average-cost method on effective fills; linear (exit-entry)*qty*multiplier, "
                    "inverse qty*multiplier*(1/entry-1/exit), sign flipped for shorts",
    "net_pnl": "gross realized PnL - fees + funding + other cashflows, in the settlement currency",
    "net_r": "net PnL / initial risk locked before the first fill; N/A without initial risk",
    "win_rate": "complete closed trades with net PnL > 0 / complete closed trades",
    "breakeven": "net PnL == 0 at 8 decimal places (counted in the win-rate denominator, not as a win)",
    "incomplete": "closed trades with an unknown fee, a missing FX rate or unknown funding; excluded from every rate",
    "expectancy": "mean net PnL per complete closed trade (per currency); expectancy_r = mean net R",
    "profit_factor": "sum of winning net PnL / |sum of losing net PnL|; null when there are no losses",
    "realized_equity_max_drawdown": "largest fall of cumulative realized net PnL from its running peak, "
                                    "complete closed trades by close time; not a mark-to-market drawdown",
    "rounding": "full precision while summing; amounts rounded once to 8 dp, ratios to 4 dp (half-even)",
    "tp1_hit_rate": "not computed: personal trades have no TP1 field, and it is not a win rate",
}
