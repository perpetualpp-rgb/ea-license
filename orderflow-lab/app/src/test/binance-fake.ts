// Synthetic Binance REST answers in Binance's exact response shapes, for tests only.
// Every number is generated from the clock (no recorded market data), and nothing touches the network.
import type { McpLike, McpResult } from "../data/connector";

export const MIN = 60_000;
const IV: Record<string, number> = { "1m": MIN, "5m": 5 * MIN, "15m": 15 * MIN, "30m": 30 * MIN, "1h": 60 * MIN };
const s = (n: number, d = 2) => n.toFixed(d);

/** a kline row: [openTime, open, high, low, close, volume, closeTime, quoteVolume, trades, takerBuyBase, takerBuyQuote, ignore] */
export function klineRow(openMs: number, ivMs: number, o: number, c: number, v: number, buy: number): unknown[] {
  const h = Math.max(o, c) + 1, l = Math.min(o, c) - 1;
  return [openMs, s(o), s(h), s(l), s(c), s(v, 3), openMs + ivMs - 1, s(v * c), 100, s(buy, 3), s(buy * c), "0"];
}

/** price path: a smooth wave around `base`, a pure function of time so pages join up */
export const pathPrice = (base: number, ms: number) => base + Math.round(Math.sin(ms / 3_600_000) * base * 0.002 * 100) / 100;

export function klines(args: Record<string, unknown>, now: number, base: number): unknown[] {
  const iv = IV[String(args.interval)];
  const limit = Number(args.limit ?? 500);
  const last = Math.floor(now / iv) * iv; // the forming bar
  const first = args.startTime != null ? Math.ceil(Number(args.startTime) / iv) * iv : last - (limit - 1) * iv;
  const rows: unknown[] = [];
  for (let t = first; t <= last && rows.length < limit; t += iv) {
    const o = pathPrice(base, t), c = pathPrice(base, t + iv);
    rows.push(klineRow(t, iv, o, c, 10, c >= o ? 6 : 4));
  }
  return rows;
}

/** aggTrades: {a, p, q, f, l, T, m}; one trade per second, id = second number, newest 2 s before now */
export function aggTrades(args: Record<string, unknown>, now: number, base: number, big: { everySec: number; qty: number } = { everySec: 50, qty: 25 }): unknown[] {
  const limit = Number(args.limit ?? 500);
  const newest = Math.floor((now - 2000) / 1000);
  const from = args.fromId != null ? Number(args.fromId) : newest - limit + 1;
  const out: unknown[] = [];
  for (let id = from; id <= newest && out.length < limit; id++) {
    const qty = id % big.everySec === 0 ? big.qty : 0.1 + (id % 7) * 0.05;
    out.push({ a: id, p: s(pathPrice(base, id * 1000)), q: s(qty, 3), f: id * 3, l: id * 3 + 2, T: id * 1000 + 250, m: id % 2 === 0 });
  }
  return out;
}

export interface WallSpec { side: "bid" | "ask"; price: number; qty: number }
/** depth: {lastUpdateId, E, T, bids, asks} (spot: no E/T); 120 levels a side `step` apart around `mid` */
export function depth(now: number, mid: number, step: number, walls: WallSpec[] = [], spot = false): Record<string, unknown> {
  const bids: string[][] = [], asks: string[][] = [];
  for (let k = 1; k <= 120; k++) {
    const bp = mid - k * step, ap = mid + k * step;
    const w = (side: string, p: number) => walls.find((x) => x.side === side && Math.abs(x.price - p) < step / 2)?.qty;
    bids.push([s(bp), s(w("bid", bp) ?? 1 + (k % 5) * 0.1, 3)]);
    asks.push([s(ap), s(w("ask", ap) ?? 1 + (k % 5) * 0.1, 3)]);
  }
  bids.push([s(mid - 121 * step), "0.000"]); // zero-size levels are dropped
  return spot ? { lastUpdateId: now, bids, asks } : { lastUpdateId: now, E: now - 5, T: now - 10, bids, asks };
}

/** openInterestStatistics: newest stat one period behind now, `points` of them 5 minutes apart */
export function oiHist(symbol: string, now: number, points = 289, jumpPct = 1.5): unknown[] {
  const end = Math.floor(now / (5 * MIN)) * 5 * MIN - 5 * MIN;
  return Array.from({ length: points }, (_, i) => {
    const t = end - (points - 1 - i) * 5 * MIN;
    const v = i === points - 1 ? 80_000 * (1 + jumpPct / 100) : 80_000;
    return { symbol, sumOpenInterest: s(v, 3), sumOpenInterestValue: s(v * 60_000), timestamp: t };
  });
}

export const markPrice = (symbol: string, now: number, mark: number) => ({
  symbol, markPrice: s(mark, 8), indexPrice: s(mark - 3, 8), estimatedSettlePrice: s(mark - 1, 8), lastFundingRate: "0.00010000",
  interestRate: "0.00010000", nextFundingTime: (Math.floor(now / 28_800_000) + 1) * 28_800_000, time: now - 1000,
});

const BASE: Record<string, number> = { BTCUSDT: 60_000, XAUUSDT: 2_400 };

export type Handler = (tool: string, args: Record<string, unknown>) => unknown;

/** a whole market: every allowlisted read answers; `override` can replace or fail any tool */
export function market(now: () => number, override: Partial<Record<string, Handler>> = {}): Handler {
  return (tool, args) => {
    if (override[tool]) return override[tool]!(tool, args);
    const sym = String(args.symbol ?? "BTCUSDT"), base = BASE[sym] ?? 100, t = now();
    switch (tool) {
      case "futures_usds.klineCandlestickData": case "spot.klines": return klines(args, t, base);
      case "futures_usds.compressedAggregateTradesList": case "spot.aggTrades": return aggTrades(args, t, base);
      case "futures_usds.orderBook": return depth(t, pathPrice(base, t), base > 10_000 ? 10 : 0.5, [{ side: "bid", price: pathPrice(base, t) - 30 * (base > 10_000 ? 10 : 0.5), qty: 40 }]);
      case "spot.depth": return depth(t, pathPrice(base, t), base > 10_000 ? 10 : 0.5, [], true);
      case "futures_usds.openInterestStatistics": return oiHist(sym, t);
      case "futures_usds.markPrice": return markPrice(sym, t, pathPrice(base, t) + 5);
      case "futures_usds.symbolPriceTicker": return { symbol: sym, price: s(pathPrice(base, t)), time: t - 500 };
      default: throw { code: "tool_error", message: `fake has no ${tool}` };
    }
  };
}

export interface Call { server: string; tool: string; input: { toolName: string; arguments: Record<string, unknown> }; options: unknown }

/** the claude.ai mcp namespace as the page sees it; answers like the Binance connector (payload, sometimes wrapped in {result}) */
export function fakeMcp(handler: Handler, wrap: "plain" | "result" | "text" = "plain"): McpLike & { calls: Call[] } {
  const calls: Call[] = [];
  return {
    calls,
    async callTool(server, tool, input, options) {
      const inp = input as Call["input"];
      calls.push({ server, tool, input: inp, options });
      const p = handler(inp.toolName, inp.arguments); // throws like the shell does
      const text = JSON.stringify(p);
      const res: McpResult = wrap === "result" ? { payload: { result: p }, content: [{ type: "text", text }] }
        : wrap === "text" ? { content: [{ type: "text", text }] } : { payload: p, content: [{ type: "text", text }] };
      return res;
    },
  };
}
