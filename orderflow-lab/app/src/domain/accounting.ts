// Personal trades entered by the user: realized PnL from fills, minus fees and funding.
// Linear contracts pay PnL in the quote currency; inverse contracts in the base currency.
import { add, cmp, dec, div, isZero, mul, neg, sub, ZERO, type Dec } from "./decimal";

export interface Fill {
  id: string;
  ts: number;
  action: "open" | "close";
  qty: string; // contracts (or base units for spot/linear with multiplier 1)
  price: string;
  fee: string;
  feeCurrency: string;
}

export interface Cashflow {
  id: string;
  ts: number;
  kind: "funding";
  amount: string; // + received, − paid
  currency: string;
}

export interface PersonalTrade {
  id: string;
  instrumentId: string;
  symbol: string;
  side: "long" | "short";
  contract: "linear" | "inverse" | "spot";
  /** base units per contract for linear; quote value per contract for inverse */
  multiplier: string;
  /** currency PnL is settled in: quote for linear/spot, base for inverse */
  settlement: string;
  initialRisk: { amount: string; currency: string } | null;
  fills: Fill[];
  cashflows: Cashflow[];
  cancelled: boolean;
  source: "manual" | "import";
  note: string;
  createdAt: number;
}

export type TradeStatus = "draft" | "open" | "partially_closed" | "closed" | "cancelled";

export interface TradeResult {
  status: TradeStatus;
  openQty: Dec;
  closedQty: Dec;
  avgEntry: Dec | null;
  gross: Dec; // realized, settlement currency
  fees: Dec;
  funding: Dec;
  net: Dec;
  netR: Dec | null; // null = N/A (no locked initial risk, or in another currency)
  complete: boolean; // false when a fee/funding is in a currency we cannot convert
  issues: string[];
  closedAt: number | null;
}

export function evaluateTrade(t: PersonalTrade): TradeResult {
  const issues: string[] = [];
  const dir = t.side === "long" ? 1n : -1n;
  const m = dec(t.multiplier);
  let openQty = ZERO;
  let closedQty = ZERO;
  let cost = ZERO; // sum(qty × price) of the open position, for average entry
  let gross = ZERO;
  let fees = ZERO;
  let complete = true;
  let closedAt: number | null = null;

  for (const f of [...t.fills].sort((a, z) => a.ts - z.ts)) {
    const q = dec(f.qty);
    const p = dec(f.price);
    if (f.fee && !isZero(dec(f.fee))) {
      if (f.feeCurrency === t.settlement) fees = add(fees, dec(f.fee));
      else {
        complete = false;
        issues.push(`ค่าธรรมเนียม ${f.fee} ${f.feeCurrency} ยังแปลงเป็น ${t.settlement} ไม่ได้`);
      }
    }
    if (f.action === "open") {
      cost = add(cost, mul(q, p));
      openQty = add(openQty, q);
      continue;
    }
    if (cmp(q, openQty) > 0) {
      issues.push("ปิดมากกว่าที่เปิดไว้");
      complete = false;
      continue;
    }
    const avg = div(cost, openQty);
    // linear: qty × multiplier × (exit − entry); inverse: qty × multiplier × (1/entry − 1/exit)
    const pnl = t.contract === "inverse"
      ? mul(mul(q, m), sub(div(dec(1), avg), div(dec(1), p)))
      : mul(mul(q, m), sub(p, avg));
    gross = add(gross, dir === 1n ? pnl : neg(pnl));
    cost = sub(cost, mul(q, avg));
    openQty = sub(openQty, q);
    closedQty = add(closedQty, q);
    closedAt = f.ts;
  }

  let funding = ZERO;
  for (const c of t.cashflows) {
    if (c.currency === t.settlement) funding = add(funding, dec(c.amount));
    else {
      complete = false;
      issues.push(`funding ${c.amount} ${c.currency} ยังแปลงเป็น ${t.settlement} ไม่ได้`);
    }
  }

  const net = add(sub(gross, fees), funding);
  const status: TradeStatus = t.cancelled && isZero(closedQty) && isZero(openQty) ? "cancelled"
    : isZero(openQty) && isZero(closedQty) ? "draft"
    : isZero(closedQty) ? "open"
    : isZero(openQty) ? "closed" : "partially_closed";

  let netR: Dec | null = null;
  if (t.initialRisk && t.initialRisk.currency === t.settlement && cmp(dec(t.initialRisk.amount), ZERO) > 0) netR = div(net, dec(t.initialRisk.amount));
  else if (t.initialRisk && t.initialRisk.currency !== t.settlement) issues.push("ความเสี่ยงเริ่มต้นคนละสกุลกับผลกำไรขาดทุน จึงคำนวณ R ไม่ได้");

  return { status, openQty, closedQty, avgEntry: isZero(openQty) ? null : div(cost, openQty), gross, fees, funding, net, netR,
    complete, issues, closedAt: status === "closed" ? closedAt : null };
}
