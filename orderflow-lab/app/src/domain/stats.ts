// Statistics over the user's own closed trades, each metric with an explicit definition.
import { add, cmp, div, toNumber, ZERO, type Dec } from "./decimal";
import { evaluateTrade, type PersonalTrade, type TradeResult } from "./accounting";

export interface TradeStats {
  closed: number;
  complete: number; // closed trades whose fees/funding could all be counted
  incomplete: number;
  wins: number;
  losses: number;
  breakeven: number;
  winRate: number | null; // wins / complete closed trades
  netTotal: Dec;
  expectancy: Dec | null; // mean net per complete closed trade
  profitFactor: number | null; // gross profit / gross loss; null with no losing trade
  avgR: number | null;
  medianR: number | null;
  rCount: number; // trades that have a locked initial risk
  realizedDrawdown: Dec; // peak-to-trough of realized equity by close time (not mark-to-market)
  open: number;
}

export function tradeStats(trades: PersonalTrade[], from?: number, to?: number): TradeStats {
  const rows = trades.map((t) => ({ t, r: evaluateTrade(t) }));
  const closed = rows.filter(({ r }) => r.status === "closed" && (from == null || r.closedAt! >= from) && (to == null || r.closedAt! <= to));
  const complete = closed.filter(({ r }) => r.complete);
  let wins = 0, losses = 0, breakeven = 0, netTotal = ZERO, gp = ZERO, gl = ZERO;
  for (const { r } of complete) {
    const c = cmp(r.net, ZERO);
    if (c > 0) { wins++; gp = add(gp, r.net); }
    else if (c < 0) { losses++; gl = add(gl, r.net); }
    else breakeven++;
    netTotal = add(netTotal, r.net);
  }
  const rs = complete.map(({ r }) => r.netR).filter((x): x is Dec => x != null).map(toNumber).sort((a, z) => a - z);
  const median = rs.length ? (rs.length % 2 ? rs[(rs.length - 1) / 2] : (rs[rs.length / 2 - 1] + rs[rs.length / 2]) / 2) : null;
  let peak = ZERO, eq = ZERO, dd = ZERO;
  for (const { r } of [...complete].sort((a, z) => a.r.closedAt! - z.r.closedAt!)) {
    eq = add(eq, r.net);
    if (cmp(eq, peak) > 0) peak = eq;
    const drop = eq - peak;
    if (cmp(drop, dd) < 0) dd = drop;
  }
  return {
    closed: closed.length, complete: complete.length, incomplete: closed.length - complete.length,
    wins, losses, breakeven, winRate: complete.length ? wins / complete.length : null,
    netTotal, expectancy: complete.length ? div(netTotal, BigInt(complete.length) * 10n ** 18n) : null,
    profitFactor: losses ? toNumber(gp) / Math.abs(toNumber(gl)) : null,
    avgR: rs.length ? rs.reduce((a, v) => a + v, 0) / rs.length : null, medianR: median, rCount: rs.length,
    realizedDrawdown: dd, open: rows.filter(({ r }) => r.status === "open" || r.status === "partially_closed").length,
  };
}

export type { TradeResult };
