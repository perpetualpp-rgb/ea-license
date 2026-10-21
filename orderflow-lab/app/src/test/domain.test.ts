import type { Bar } from "../data/types";
import { describe, expect, it } from "vitest";
import { dec, div, mul, toFixed } from "../domain/decimal";
import { evaluateTrade, type PersonalTrade } from "../domain/accounting";
import { tradeStats } from "../domain/stats";
import { aggregate, cvd, niceStep, profile, vwap, vwapBands, roundLevels, prevSession, stepOnBars } from "../domain/indicators";
import { dedupeKey, evaluateRule, mergeEvents, type AlertRule, type EvalContext } from "../domain/alerts";
import type { StreamQuality } from "../data/types";

const T0 = Date.UTC(2026, 9, 18, 3, 0, 0);

function trade(over: Partial<PersonalTrade>): PersonalTrade {
  return { id: "t", instrumentId: "x", symbol: "BTCUSDT", side: "long", contract: "linear", multiplier: "1", settlement: "USDT",
    initialRisk: { amount: "100", currency: "USDT" }, fills: [], cashflows: [], cancelled: false, source: "manual", note: "", createdAt: T0, ...over };
}
const f = (id: string, ts: number, action: "open" | "close", qty: string, price: string, fee = "0") => ({ id, ts, action, qty, price, fee, feeCurrency: "USDT" });

describe("decimal", () => {
  it("adds without float error and rounds half away from zero", () => {
    expect(toFixed(dec("0.1") + dec("0.2"), 2)).toBe("0.30");
    expect(toFixed(dec("2.345"), 2)).toBe("2.35");
    expect(toFixed(dec("-2.345"), 2)).toBe("-2.35");
    expect(toFixed(div(dec("1"), dec("3")), 6)).toBe("0.333333");
    expect(toFixed(mul(dec("1.5"), dec("-2")), 1)).toBe("-3.0");
  });
});

describe("personal trade accounting", () => {
  it("partial exits, fees and funding on a linear long", () => {
    // open 2 @100 (fee 0.2), sell 1 @110 at TP1 (fee 0.11), sell 1 @100 back at entry (fee 0.1), pay 0.05 funding
    const r = evaluateTrade(trade({ fills: [f("a", 1, "open", "2", "100", "0.2"), f("b", 2, "close", "1", "110", "0.11"), f("c", 3, "close", "1", "100", "0.1")],
      cashflows: [{ id: "fu", ts: 2, kind: "funding", amount: "-0.05", currency: "USDT" }], initialRisk: { amount: "20", currency: "USDT" } }));
    expect(r.status).toBe("closed");
    expect(toFixed(r.gross, 2)).toBe("10.00");
    expect(toFixed(r.fees, 2)).toBe("0.41");
    expect(toFixed(r.net, 2)).toBe("9.54");
    expect(toFixed(r.netR!, 3)).toBe("0.477");
  });

  it("partially closed and open states, average entry across partial fills", () => {
    const open = evaluateTrade(trade({ fills: [f("a", 1, "open", "1", "100"), f("b", 2, "open", "3", "104")] }));
    expect(open.status).toBe("open");
    expect(toFixed(open.avgEntry!, 2)).toBe("103.00");
    const part = evaluateTrade(trade({ fills: [f("a", 1, "open", "4", "103"), f("b", 2, "close", "1", "113")] }));
    expect(part.status).toBe("partially_closed");
    expect(toFixed(part.gross, 2)).toBe("10.00");
  });

  it("short PnL, contract multiplier and draft/cancelled", () => {
    const r = evaluateTrade(trade({ side: "short", multiplier: "0.1", fills: [f("a", 1, "open", "10", "2000"), f("b", 2, "close", "10", "1990")] }));
    expect(toFixed(r.gross, 2)).toBe("10.00"); // 10 contracts × 0.1 oz × 10
    expect(evaluateTrade(trade({})).status).toBe("draft");
    expect(evaluateTrade(trade({ cancelled: true })).status).toBe("cancelled");
  });

  it("inverse contract pays in base currency", () => {
    // long 1000 USD of BTC at 50,000, close at 55,000 → 1000 × (1/50000 − 1/55000) = 0.0018181818 BTC
    const r = evaluateTrade(trade({ contract: "inverse", multiplier: "1000", settlement: "BTC", initialRisk: null,
      fills: [{ ...f("a", 1, "open", "1", "50000"), feeCurrency: "BTC" }, { ...f("b", 2, "close", "1", "55000"), feeCurrency: "BTC" }] }));
    expect(toFixed(r.gross, 10)).toBe("0.0018181818");
    expect(r.netR).toBeNull();
  });

  it("R is N/A without a locked risk, and fees in another currency make the trade incomplete", () => {
    const noRisk = evaluateTrade(trade({ initialRisk: null, fills: [f("a", 1, "open", "1", "100"), f("b", 2, "close", "1", "101")] }));
    expect(noRisk.netR).toBeNull();
    const bnb = evaluateTrade(trade({ fills: [f("a", 1, "open", "1", "100"), { ...f("b", 2, "close", "1", "101", "0.001"), feeCurrency: "BNB" }] }));
    expect(bnb.complete).toBe(false);
    expect(bnb.issues.length).toBe(1);
  });
});

describe("statistics", () => {
  it("win rate counts net > 0 over complete closed trades; breakeven and incomplete are separate", () => {
    const win = trade({ id: "w", fills: [f("a", 1, "open", "1", "100"), f("b", 10, "close", "1", "110")] });
    const loss = trade({ id: "l", fills: [f("a", 2, "open", "1", "100"), f("b", 20, "close", "1", "95")] });
    const be = trade({ id: "e", fills: [f("a", 3, "open", "1", "100"), f("b", 30, "close", "1", "100")] });
    const inc = trade({ id: "i", fills: [f("a", 4, "open", "1", "100"), { ...f("b", 40, "close", "1", "120", "1"), feeCurrency: "BNB" }] });
    const open = trade({ id: "o", fills: [f("a", 5, "open", "1", "100")] });
    const s = tradeStats([win, loss, be, inc, open]);
    expect([s.closed, s.complete, s.incomplete, s.wins, s.losses, s.breakeven, s.open]).toEqual([4, 3, 1, 1, 1, 1, 1]);
    expect(s.winRate).toBeCloseTo(1 / 3);
    expect(toFixed(s.netTotal, 2)).toBe("5.00");
    expect(s.profitFactor).toBe(2);
    expect(toFixed(s.realizedDrawdown, 2)).toBe("-5.00");
    expect(s.medianR).toBeCloseTo(0);
  });

  it("filters by close time over the whole set", () => {
    const a = trade({ id: "a", fills: [f("x", 1, "open", "1", "100"), f("y", 100, "close", "1", "110")] });
    const b = trade({ id: "b", fills: [f("x", 1, "open", "1", "100"), f("y", 900, "close", "1", "90")] });
    expect(tradeStats([a, b], 0, 500).closed).toBe(1);
  });
});

describe("indicators", () => {
  const bars = [
    { t: 0, o: 10, h: 11, l: 9, c: 10, v: 10, buyV: 7 },
    { t: 60, o: 10, h: 12, l: 10, c: 12, v: 20, buyV: 5 },
    { t: 120, o: 12, h: 12, l: 11, c: 11, v: 10, buyV: null },
  ];
  it("CVD starts at the anchor and stops (null) where the aggressor split is unknown", () => {
    expect(cvd(bars, 60)).toEqual([null, -10, null]);
    expect(cvd(bars, 0)).toEqual([4, -6, null]);
  });
  it("VWAP uses typical price × volume from the anchor", () => {
    expect(vwap(bars, 0)[1]).toBeCloseTo((10 * 10 + (34 / 3) * 20) / 30);
  });
  it("aggregation keeps volume and loses the split if any minute lacks it", () => {
    const a = aggregate(bars, 180);
    expect(a).toHaveLength(1);
    expect(a[0]).toMatchObject({ o: 10, h: 12, l: 9, c: 11, v: 40, buyV: null });
  });
  it("profile POC and value area", () => {
    const p = profile([{ t: 0, o: 1, h: 1.9, l: 1, c: 1.5, v: 100, buyV: 50 }, { t: 60, o: 3, h: 3.5, l: 3, c: 3.2, v: 10, buyV: 5 }], 1);
    expect(p.poc).toBe(1.5);
    expect(p.vaLow).toBe(1);
  });
});

describe("alert rules", () => {
  const ok: StreamQuality[] = ["trades", "book", "kline", "oi", "mark", "liquidations"].map((s) => ({ stream: s as StreamQuality["stream"], status: "ok", lastAt: T0 }));
  const rule: AlertRule = { id: "r1", instrumentId: "x", metric: "oi_change_pct", comparator: ">", threshold: 2, window: "5m", cooldownMin: 15, channel: "in_app", enabled: true, createdAt: T0 };
  const ctx = (over: Partial<EvalContext>): EvalContext => ({ now: T0, market: "perpetual", quality: ok, measure: () => 2.1, lastFiredAt: null, units: { qty: "BTC", quote: "USDT" }, symbol: "BTCUSDT", ...over });

  it("fires with the measured value and threshold", () => {
    const s = evaluateRule(rule, ctx({}));
    expect(s.state).toBe("triggered");
    if (s.state === "triggered") expect(s.event).toMatchObject({ measured: 2.1, threshold: 2, live: true, dedupeKey: dedupeKey(rule, T0) });
  });
  it("pauses when the stream it depends on is unhealthy, and is unsupported on spot", () => {
    const stale = ok.map((q) => (q.stream === "oi" ? { ...q, status: "stale" as const } : q));
    expect(evaluateRule(rule, ctx({ quality: stale })).state).toBe("paused");
    expect(evaluateRule(rule, ctx({ market: "spot" })).state).toBe("unsupported");
    const wall = { ...rule, metric: "wall_size" as const };
    expect(evaluateRule(wall, ctx({ quality: ok.map((q) => (q.stream === "book" ? { ...q, status: "book_resync" as const } : q)) })).state).toBe("paused");
  });
  it("no data is not zero, cooldown blocks re-firing, duplicates are merged by key", () => {
    expect(evaluateRule(rule, ctx({ measure: () => null })).state).toBe("no_data");
    expect(evaluateRule(rule, ctx({ lastFiredAt: T0 - 60_000 })).state).toBe("cooldown");
    const s1 = evaluateRule(rule, ctx({ now: T0 + 10_000 }));
    const s2 = evaluateRule(rule, ctx({ now: T0 + 20_000 })); // same 5-minute bucket, e.g. after a reconnect
    if (s1.state !== "triggered" || s2.state !== "triggered") throw new Error("expected triggers");
    expect(mergeEvents(mergeEvents([], [s1.event]), [s2.event])).toHaveLength(1);
  });
});

describe("chart layers (desk parity)", () => {
  const bar = (t: number, c: number, v = 10): Bar => ({ t, o: c, h: c + 1, l: c - 1, c, v, buyV: v / 2 });
  it("Assist bands: VWAP ± k·σ with volume-weighted σ of typical price", () => {
    const b = vwapBands([bar(0, 100, 10), bar(60, 110, 30)], 0);
    expect(b[0]).toEqual({ vwap: 100, sd: 0 });
    expect(b[1]!.vwap).toBeCloseTo(107.5, 9);
    expect(b[1]!.sd).toBeCloseTo(Math.sqrt((10 * 7.5 ** 2 + 30 * 2.5 ** 2) / 40), 9);
  });
  it("round numbers: BTC ≈ 500 steps, gold ≈ 25, never more than 24 levels", () => {
    expect(roundLevels(81_000, 82_100).step).toBe(500);
    expect(roundLevels(81_000, 82_100).levels).toEqual([81_000, 81_500, 82_000]);
    expect(roundLevels(4_150, 4_210).step).toBe(25);
    expect(roundLevels(60_000, 90_000).levels.length).toBeLessThanOrEqual(24);
    expect([niceStep(82_000 / 5000), niceStep(4_157 / 5000), niceStep(1)]).toEqual([10, 0.5, 1]); // same bins as the server's profile
  });
  it("Range needs ≥ 80 % of the previous UTC day loaded, and says so otherwise", () => {
    const day = 86_400 * 20_000;
    const full = Array.from({ length: 2 * 288 }, (_, i) => bar(day - 86_400 + i * 300, 100 + (i % 10)));
    const r = prevSession(full, 1);
    expect("reason" in r).toBe(false);
    if (!("reason" in r)) { expect(r.hi).toBe(110); expect(r.lo).toBe(99); expect(r.start).toBe(day - 86_400); }
    const partial = full.slice(150);
    expect(prevSession(partial, 1)).toMatchObject({ reason: expect.stringContaining("3 วัน") });
  });
  it("OI is stepped onto bars by close time, null before the first reading", () => {
    const bars = [bar(0, 1), bar(900, 1), bar(1800, 1)];
    expect(stepOnBars(bars, [{ t: 1200, v: 5 }, { t: 1500, v: 6 }, { t: 2400, v: 7 }], 900)).toEqual([null, 6, 7]);
  });
});
