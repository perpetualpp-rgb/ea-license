// ConnectorSource with a fake claude.ai mcp namespace answering in Binance's exact REST shapes.
// No network: every answer is synthetic (src/test/binance-fake.ts).
import { describe, expect, it, vi } from "vitest";
import {
  BIN_ALLOW, ConnectorError, ConnectorSource, DENY, freshness, mapMcpError, parseAggTrades, parseDepth, parseKlines, parseMark, parseOi,
  seriesWindowMs, unwrapPayload, type McpLike,
} from "../data/connector";
import { SourceError, errorText } from "../data/source";
import { aggTrades, depth, fakeMcp, klineRow, klines, market, markPrice, MIN, oiHist, type Handler } from "./binance-fake";
import deskRaw from "../../../desk/template.html?raw";

const T0 = Date.UTC(2026, 9, 18, 3, 0, 0); // 10:00 UTC+7
const PERP = "binance-um-btcusdt", GOLD = "binance-um-xauusdt", SPOT = "binance-spot-btcusdt";

function setup(over: Partial<Record<string, Handler>> = {}, wrap: "plain" | "result" | "text" = "plain") {
  let now = T0;
  const clock = { now: () => now, advance: (ms: number) => { now += ms; } };
  const mcp = fakeMcp(market(clock.now, over), wrap);
  const issues: unknown[] = [];
  const src = new ConnectorSource(async () => mcp, { now: clock.now, onIssue: (i) => issues.push(i), retryDelayMs: 0 });
  return { src, mcp, clock, issues };
}

describe("allowlist", () => {
  it("is exactly the desk's set of public market-data tools", () => {
    const block = deskRaw.match(/const BIN_ALLOW = new Set\(\[([\s\S]*?)\]\)/)![1];
    const desk = [...block.matchAll(/"([^"]+)"/g)].map((m) => m[1]).sort();
    expect([...BIN_ALLOW].sort()).toEqual(desk);
    expect(BIN_ALLOW.size).toBe(11);
    for (const t of BIN_ALLOW) expect(t).not.toMatch(/order(?!Book)|account|transfer|withdraw|borrow|leverage|margin|convert|cancel/i);
  });

  it("rejects anything else before calling the connector", async () => {
    const { src, mcp } = setup();
    for (const t of ["spot.newOrder", "futures_usds.newOrder", "futures_usds.cancelOrder", "spot.getAccount", "futures_usds.accountInformationV3",
      "wallet.userUniversalTransfer", "wallet.withdrawHistory", "convert.acceptQuote", "margin.marginAccountBorrowRepay", "futures_usds.changeInitialLeverage", "tool_search", ""]) {
      const err = (await src.bin(t, { symbol: "BTCUSDT" }).catch((e) => e)) as ConnectorError;
      expect(err).toBeInstanceOf(ConnectorError);
      expect(err.connectorCode).toBe("blocked_tool");
      expect(err.code).toBe("not_supported");
    }
    expect(mcp.calls).toHaveLength(0);
  });

  it("a full visit only ever sends allowlisted tools through Binance tool_execute, uncached", async () => {
    const { src, mcp } = setup();
    for (const id of [PERP, GOLD, SPOT]) {
      await src.quote(id);
      await src.series(id, "5m", "24h");
      await src.details(id, "1m", "6h");
      await src.observations(id);
    }
    expect(mcp.calls.length).toBeGreaterThan(10);
    for (const c of mcp.calls) {
      expect(c.server).toBe("Binance");
      expect(c.tool).toBe("tool_execute");
      expect(BIN_ALLOW.has(c.input.toolName)).toBe(true);
      expect(c.options).toEqual({ cache: false });
    }
    // spot never asks perpetual-only endpoints
    const spotCalls = mcp.calls.filter((c) => c.input.toolName.startsWith("spot."));
    expect(spotCalls.length).toBeGreaterThan(0);
  });
});

describe("payloads", () => {
  it("unwraps like the desk: payload, {result}, JSON text; Binance errors become tool_error", () => {
    expect(unwrapPayload({ payload: [1, 2] })).toEqual([1, 2]);
    expect(unwrapPayload({ payload: { result: "[3]" } })).toEqual([3]);
    expect(unwrapPayload({ content: [{ type: "text", text: '{"bids":[],"asks":[]}' }] })).toEqual({ bids: [], asks: [] });
    expect(() => unwrapPayload({ payload: { code: -1121, msg: "Invalid symbol." } })).toThrow();
    try { unwrapPayload({ payload: { code: -1121, msg: "Invalid symbol." } }); } catch (e) { expect(e).toMatchObject({ code: "tool_error", message: "-1121 Invalid symbol." }); }
    try { unwrapPayload({ payload: "upstream said no" }); } catch (e) { expect(e).toMatchObject({ code: "tool_error" }); }
  });

  it("klines → bars with exact taker-buy volume", () => {
    const rows = [klineRow(T0, MIN, 100, 101, 10, 6), klineRow(T0 - MIN, MIN, 99, 100, 5, 2)];
    const bars = parseKlines(rows);
    expect(bars).toEqual([
      { t: (T0 - MIN) / 1000, o: 99, h: 101, l: 98, c: 100, v: 5, buyV: 2 },
      { t: T0 / 1000, o: 100, h: 102, l: 99, c: 101, v: 10, buyV: 6 },
    ]);
    expect(parseKlines([[T0, "1", "2", "0.5", "1.5", "3"]])[0].buyV).toBeNull(); // no aggressor split → null, never guessed
    expect(() => parseKlines({ code: 1 })).toThrow();
  });

  it("aggTrades: m = buyer is maker → the taker sold", () => {
    const t = parseAggTrades([{ a: 7, p: "60000.10", q: "0.250", f: 1, l: 2, T: T0, m: true }, { a: 6, p: "60000.00", q: "1.000", f: 0, l: 0, T: T0 - 5, m: false }]);
    expect(t).toEqual([{ id: 6, ts: T0 - 5, price: 60000, size: 1, side: "buy" }, { id: 7, ts: T0, price: 60000.1, size: 0.25, side: "sell" }]);
  });

  it("depth: futures carry exchange time T; spot has none (receive time, flagged); zero levels dropped, sides sorted", () => {
    const f = parseDepth(depth(T0, 60000, 10), T0 + 99, "BTCUSDT");
    expect(f.ts).toBe(T0 - 10);
    expect(f.exchangeTime).toBe(true);
    expect(f.bids[0][0]).toBeGreaterThan(f.bids[1][0]);
    expect(f.asks[0][0]).toBeLessThan(f.asks[1][0]);
    expect(f.bids.every((l) => l[1] > 0)).toBe(true);
    const sp = parseDepth(depth(T0, 60000, 10, [], true), T0 + 99, "BTCUSDT");
    expect(sp.ts).toBe(T0 + 99);
    expect(sp.exchangeTime).toBe(false);
  });

  it("markPrice: estimated funding in %, next funding time; object or array", () => {
    const m = parseMark(markPrice("BTCUSDT", T0, 60005), "BTCUSDT");
    expect(m).toMatchObject({ mark: 60005, index: 60002, nextAt: (Math.floor(T0 / 28_800_000) + 1) * 28_800_000, time: T0 - 1000 });
    expect(m.ratePct).toBeCloseTo(0.01, 10);
    expect(parseMark([markPrice("ETHUSDT", T0, 1), markPrice("BTCUSDT", T0, 2)], "BTCUSDT").mark).toBe(2);
  });

  it("openInterestStatistics: base-asset OI series, oldest first", () => {
    const s = parseOi(oiHist("BTCUSDT", T0, 13));
    expect(s).toHaveLength(13);
    expect(s[12].t - s[11].t).toBe(300);
    expect(s[0].v).toBe(80000);
  });
});

describe("quote", () => {
  it("last price and exchange time from the newest aggTrade; 24 h change from the 1m kline 24 h back; 30m spark", async () => {
    const { src, mcp } = setup();
    const q = await src.quote(PERP);
    expect(q.priceType).toBe("last_trade");
    expect(q.freshness).toEqual({ kind: "live", asOf: T0 - 2000 + 250 });
    const ref = mcp.calls.find((c) => c.input.arguments.interval === "1m" && c.input.arguments.limit === 1)!;
    expect(ref.input.arguments.startTime).toBe(T0 - 86_400_000);
    const refOpen = Number((klines(ref.input.arguments, T0, 60000)[0] as string[])[1]);
    expect(q.changePct).toBeCloseTo((q.price! / refOpen - 1) * 100, 10);
    expect(q.spark.length).toBeGreaterThanOrEqual(48);
    expect(mcp.calls.some((c) => c.input.arguments.interval === "30m")).toBe(true);
  });

  it("24 h change is null when that kline is missing or fails — never another window", async () => {
    const empty = setup({ "futures_usds.klineCandlestickData": (_t, a) => (a.limit === 1 ? [] : klines(a, T0, 60000)) });
    expect((await empty.src.quote(PERP)).changePct).toBeNull();
    const shifted = setup({ "futures_usds.klineCandlestickData": (_t, a) => (a.limit === 1 ? klines({ ...a, startTime: Number(a.startTime) + 5 * MIN }, T0, 60000) : klines(a, T0, 60000)) });
    expect((await shifted.src.quote(PERP)).changePct).toBeNull();
    const failing = setup({ "futures_usds.klineCandlestickData": (_t, a) => { if (a.limit === 1) throw { code: "tool_error", message: "-1100 bad" }; return klines(a, T0, 60000); } });
    const q = await failing.src.quote(PERP);
    expect(q.changePct).toBeNull();
    expect(q.price).not.toBeNull();
  });
});

describe("series", () => {
  it("klines at the chosen timeframe with exact buyV, CVD anchored at 07:00 UTC+7", async () => {
    const { src } = setup();
    const s = await src.series(PERP, "5m", "24h");
    expect(s.bars.length).toBe(24 * 12 + 1);
    expect(s.bars.every((b) => b.buyV != null && b.t % 300 === 0)).toBe(true);
    expect(s.cvdAnchor).toEqual({ label: "เริ่มนับ 07:00 UTC+7", startT: Math.floor(T0 / 86_400_000) * 86400 });
    expect(s.freshness.kind).toBe("live");
  });

  it("pages when the lookback needs more than one call, then only re-reads the forming bar", async () => {
    const { src, mcp, clock } = setup();
    const s = await src.series(SPOT, "1m", "3d");
    const pages = mcp.calls.filter((c) => c.input.toolName === "spot.klines" && c.input.arguments.interval === "1m");
    expect(pages.length).toBe(5); // 4321 bars, 1000 per spot call
    expect(s.bars.length).toBe(3 * 1440 + 1);
    expect(s.bars.every((b, i) => i === 0 || b.t - s.bars[i - 1].t === 60)).toBe(true);
    expect(s.cvdAnchor.label).toMatch(/^เริ่มนับ 07:00 UTC\+7$/);
    clock.advance(30_000);
    mcp.calls.length = 0;
    await src.series(SPOT, "1m", "3d");
    const again = mcp.calls.filter((c) => c.input.toolName === "spot.klines");
    expect(again).toHaveLength(1);
    expect(Number(again[0].input.arguments.limit)).toBeLessThanOrEqual(3);
    const fut = setup();
    await fut.src.series(PERP, "1m", "3d");
    expect(fut.mcp.calls.filter((c) => c.input.toolName === "futures_usds.klineCandlestickData" && c.input.arguments.interval === "1m")).toHaveLength(3);
  });

  it("6 h lookback starts CVD at the first bar and says so", async () => {
    const { src, clock } = setup();
    clock.advance(5 * 3600_000); // 15:00 UTC+7: the 6 h window starts at 09:00 UTC+7, after the 07:00 session start
    const s = await src.series(PERP, "1m", "6h");
    expect(s.cvdAnchor.startT).toBe(s.bars[0].t);
    expect(s.cvdAnchor.label).toContain("แท่งแรกของช่วงที่เลือก");
  });
});

describe("freshness", () => {
  it("live only within 2 bars and never more than 120 s; otherwise stale; never demo", () => {
    expect(freshness(T0 - 119_000, T0)).toEqual({ kind: "live", asOf: T0 - 119_000 });
    expect(freshness(T0 - 121_000, T0).kind).toBe("stale");
    expect(freshness(null, T0).kind).toBe("unavailable");
    expect(seriesWindowMs("1m")).toBe(120_000);
    expect(seriesWindowMs("1h")).toBe(120_000);
  });

  it("a quiet market (newest trade 3 min old) is stale, even on a 1 h chart", async () => {
    const quiet: Handler = (_t, a) => aggTrades(a, T0 - 3 * MIN, 60000);
    const { src } = setup({ "futures_usds.compressedAggregateTradesList": quiet });
    expect((await src.quote(PERP)).freshness.kind).toBe("stale");
    expect((await src.series(PERP, "1h", "24h")).freshness.kind).toBe("live"); // the 1h kline opened 0 s ago — at T0 exactly
    const late = setup({ "futures_usds.compressedAggregateTradesList": quiet });
    late.clock.advance(10 * MIN);
    const s = await late.src.series(PERP, "1h", "24h");
    expect(s.freshness.kind).toBe("stale");
    const d = await late.src.details(PERP, "1h", "24h");
    expect(d.quality.find((q) => q.stream === "trades")?.status).toBe("stale");
  });
});

describe("details", () => {
  it("perpetual: walls, big trades with the stated threshold, profile, OI in base asset with 5m/1h change, estimated funding, typed prices", async () => {
    const { src } = setup();
    const d = await src.details(PERP, "1m", "6h");
    expect(d.walls.length).toBeGreaterThan(0);
    expect(d.walls[0]).toMatchObject({ side: "bid", status: "new", ageSec: 0 });
    expect(d.wallMethod).toContain("อายุนับจากที่หน้านี้เห็นครั้งแรก");
    expect(d.bigTrades.length).toBeGreaterThan(0);
    expect(d.bigTrades.every((t) => t.size >= 25)).toBe(true);
    expect(d.bigMethod).toMatch(/เปอร์เซ็นไทล์ที่ 99 \(≥ 25\.0 BTC\) ของ 1,000 aggTrade/);
    expect(d.oi).toMatchObject({ unit: "BTC", value: 81200 });
    expect(d.oi!.change5mPct).toBeCloseTo(1.5, 6);
    expect(d.oi!.change1hPct).toBeCloseTo(1.5, 6);
    expect(d.oi!.funding).toMatchObject({ kind: "estimated", nextAt: (Math.floor(T0 / 28_800_000) + 1) * 28_800_000 });
    expect(d.oi!.funding!.ratePct).toBeCloseTo(0.01, 10);
    expect(d.prices.map((p) => p.type)).toEqual(["last_trade", "mark", "index"]);
    expect(d.prices[1].value).not.toBeNull();
    expect(d.prices[1].value).not.toBe(d.prices[2].value);
    expect(d.profile?.poc).not.toBeNull();
    expect(d.quality.map((q) => [q.stream, q.status])).toEqual([["trades", "ok"], ["book", "ok"], ["kline", "ok"], ["oi", "ok"], ["mark", "ok"], ["liquidations", "unavailable"]]);
  });

  it("gold uses oz for sizes and XAU for OI; spot has no OI, funding, mark or index", async () => {
    const { src, mcp } = setup();
    const g = await src.details(GOLD, "5m", "24h");
    expect(g.oi?.unit).toBe("XAU");
    const sp = await src.details(SPOT, "5m", "24h");
    expect(sp.oi).toBeNull();
    expect(sp.prices.filter((p) => p.type !== "last_trade").every((p) => p.value === null)).toBe(true);
    expect(sp.quality.filter((q) => ["oi", "mark", "liquidations"].includes(q.stream)).every((q) => q.status === "unavailable")).toBe(true);
    const oiCalls = mcp.calls.filter((c) => c.input.toolName === "futures_usds.openInterestStatistics" || c.input.toolName === "futures_usds.markPrice");
    expect(oiCalls.length).toBeGreaterThan(0);
    expect(oiCalls.every((c) => c.input.arguments.symbol === "XAUUSDT")).toBe(true); // the spot instrument never asked for OI or funding
  });

  it("one failing stream is contained in its own quality row", async () => {
    const { src } = setup({ "futures_usds.markPrice": () => { throw { code: "server_unavailable", message: "502" }; } });
    const d = await src.details(PERP, "1m", "6h");
    const mark = d.quality.find((q) => q.stream === "mark")!;
    expect(mark.status).toBe("disconnected");
    expect(mark.note).toContain("Binance ไม่ตอบชั่วคราว");
    expect(d.oi?.funding).toBeNull();
    expect(d.oi?.value).toBe(81200);
    expect(d.prices[1].value).toBeNull();
  });
});

describe("wall tracker across polls", () => {
  const book = (T: number, wallQty: number | null, bestBid = 60000) => ({
    lastUpdateId: T, E: T, T,
    bids: Array.from({ length: 60 }, (_, k) => [String(bestBid - k * 10), String(k === 20 && wallQty != null ? wallQty : 1)]),
    asks: Array.from({ length: 60 }, (_, k) => [String(60010 + k * 10), "1"]),
  });

  it("new → adding with real age, then removed after missing 3 polls", async () => {
    let T = T0, qty: number | null = 50, best = 60000;
    const { src, clock } = setup({ "futures_usds.orderBook": () => book(T, qty, best) });
    const w1 = (await src.details(PERP, "1m", "6h")).walls.find((w) => w.price === 59800)!;
    expect(w1).toMatchObject({ side: "bid", status: "new", ageSec: 0, size: 50 });

    clock.advance(30_000); T += 30_000; qty = 70; // +40 %
    const w2 = (await src.details(PERP, "1m", "6h")).walls.find((w) => w.price === 59800)!;
    expect(w2).toMatchObject({ status: "adding", ageSec: 30, size: 70 });

    clock.advance(30_000); T += 30_000;
    expect((await src.details(PERP, "1m", "6h")).walls.find((w) => w.price === 59800)?.status).toBe("holding");

    qty = null; // the wall is gone while price stays above it
    for (let i = 0; i < 3; i++) { clock.advance(10_000); T += 10_000; await src.details(PERP, "1m", "6h"); }
    const d = await src.details(PERP, "1m", "6h");
    expect(d.walls.find((w) => w.price === 59800)).toBeUndefined();
    expect(d.wallEvents[0]).toMatchObject({ side: "bid", price: 59800, size: 70, outcome: "removed", at: T });
  });

  it("price reaching the wall's level is reported as price_reached; a long pause closes walls as unknown", async () => {
    let T = T0, qty: number | null = 50, best = 60000;
    const { src, clock } = setup({ "futures_usds.orderBook": () => book(T, qty, best) });
    await src.details(PERP, "1m", "6h");
    clock.advance(10_000); T += 10_000;
    await src.details(PERP, "1m", "6h");
    qty = null; best = 59790; // best bid now below the old wall
    for (let i = 0; i < 3; i++) { clock.advance(10_000); T += 10_000; await src.details(PERP, "1m", "6h"); }
    expect((await src.details(PERP, "1m", "6h")).wallEvents[0]).toMatchObject({ price: 59800, outcome: "price_reached" });

    qty = 50; best = 60000;
    clock.advance(10_000); T += 10_000; await src.details(PERP, "1m", "6h");
    clock.advance(10_000); T += 10_000; await src.details(PERP, "1m", "6h");
    clock.advance(5 * MIN); T += 5 * MIN; // nobody looked for 5 minutes
    const d = await src.details(PERP, "1m", "6h");
    expect(d.wallEvents[0]).toMatchObject({ price: 59800, outcome: "unknown" });
    expect(d.walls.find((w) => w.price === 59800)).toMatchObject({ status: "new", ageSec: 0 });
    expect(d.quality.find((q) => q.stream === "book")?.note).toContain("เริ่มนับใหม่");
  });
});

describe("observations", () => {
  it("≤ 3 facts with units and window: CVD, OI, wall on a perpetual; CVD, big trade, wall on spot", async () => {
    const { src } = setup();
    const p = await src.observations(PERP);
    expect(p.map((o) => o.kind)).toEqual(["cvd", "oi", "wall"]);
    expect(p[0].measured).toMatch(/^CVD [+−]\d+\.\d BTC ใน 5 นาที/);
    expect(p[1].measured).toBe("OI +1.50% ใน 5 นาที (หน่วย BTC)");
    expect(p[2].measured).toMatch(/BTC ที่ [\d,.]+ USDT · เพิ่งเห็นครั้งแรก$/);
    const s = await src.observations(SPOT);
    expect(s.map((o) => o.kind)).toEqual(["cvd", "bigtrade"]); // the synthetic spot book has no wall
    expect(s[1].measured).toMatch(/^(ซื้อ|ขาย) 25\.00 BTC ที่ [\d,.]+ USDT · เกณฑ์ ≥ เปอร์เซ็นไทล์ 99 ของ 1,000 ไม้/);
    expect(await src.alertHistory()).toEqual([]);
  });
});

describe("errors", () => {
  const cases: [string, SourceError["code"], string][] = [
    ["server_not_connected", "not_configured", "ยังไม่ได้เชื่อม connector Binance"],
    ["needs_reauth", "unauthorized", "การเชื่อมต่อ Binance หมดอายุ"],
    ["not_in_manifest", "unauthorized", "เปิดเมนู Permissions"],
    ["selection_required", "not_configured", "มากกว่าหนึ่งตัว"],
    ["approval_required", "unauthorized", "อนุญาตและดึงข้อมูล"],
    ["blocked_by_policy", "unauthorized", "องค์กรของคุณปิดการใช้ Binance"],
    ["server_unavailable", "network", "ไม่ตอบชั่วคราว"],
    ["rate_limited", "rate_limited", "ไม่ตอบชั่วคราว"],
    ["not_granted", "not_configured", "เปิดหน้านี้ใน claude.ai"],
    ["upstream_error", "network", "upstream_error"],
    ["something_new", "server", "something_new"],
  ];
  it.each(cases)("%s → %s with the desk's wording", (code, sc, text) => {
    const e = mapMcpError({ code, message: "x" });
    expect(e.code).toBe(sc);
    expect(errorText(e)).toContain(text);
  });

  it("Binance error codes inside tool_error: -1003 → rate_limited, -1121 → not_supported", () => {
    expect(mapMcpError({ code: "tool_error", message: "-1003 Too many requests" }).code).toBe("rate_limited");
    expect(mapMcpError({ code: "tool_error", message: "-1121 Invalid symbol." }).code).toBe("not_supported");
    expect(mapMcpError({ code: "tool_error", message: "-1100 Illegal characters" }).code).toBe("server");
  });

  it("a denial is reported as a connector issue and the call rejects with a SourceError", async () => {
    const { src, issues } = setup({ "futures_usds.compressedAggregateTradesList": () => { throw { code: "server_not_connected", message: "no Binance" }; },
      "futures_usds.klineCandlestickData": () => { throw { code: "server_not_connected", message: "no Binance" }; } });
    const err = await src.quote(PERP).catch((e) => e);
    expect(err).toBeInstanceOf(SourceError);
    expect(err.code).toBe("not_configured");
    expect(issues[0]).toMatchObject({ code: "server_not_connected", approve: false, retry: true });
    expect(DENY.has("approval_required")).toBe(true);
  });

  it("approval_required asks for the approve button; use('mcp') → null explains to open in claude.ai", async () => {
    const appr = setup({ "futures_usds.symbolPriceTicker": () => { throw { code: "approval_required", message: "declined" }; } });
    expect(await appr.src.probe()).toMatchObject({ code: "approval_required", approve: true });
    const issues: unknown[] = [];
    const none = new ConnectorSource(async () => null, { now: () => T0, onIssue: (i) => issues.push(i) });
    const e = await none.series(PERP, "5m", "24h").catch((x) => x);
    expect(e.code).toBe("not_configured");
    expect(errorText(e)).toContain("เปิดหน้านี้ใน claude.ai");
    expect(issues[0]).toMatchObject({ code: "no_mcp" });
  });

  it("a failed refresh keeps the last good values, which turn stale by their own exchange time", async () => {
    let down = false;
    const fail = (h: Handler): Handler => (t, a) => { if (down) throw { code: "server_unavailable", message: "502" }; return h(t, a); };
    const { src, clock } = setup({
      "futures_usds.compressedAggregateTradesList": fail((_t, a) => aggTrades(a, clock.now(), 60000)),
      "futures_usds.klineCandlestickData": fail((_t, a) => klines(a, clock.now(), 60000)),
    });
    const q1 = await src.quote(PERP);
    const s1 = await src.series(PERP, "5m", "24h");
    down = true;
    clock.advance(3 * MIN);
    const q2 = await src.quote(PERP);
    expect(q2.price).toBe(q1.price);
    expect(q2.freshness).toEqual({ kind: "stale", asOf: q1.freshness.asOf });
    const s2 = await src.series(PERP, "5m", "24h");
    expect(s2.bars).toEqual(s1.bars);
    expect(s2.freshness.kind).toBe("stale");
    const d = await src.details(PERP, "5m", "24h");
    expect(d.quality.find((x) => x.stream === "trades")).toMatchObject({ status: "disconnected" });
    expect(d.quality.find((x) => x.stream === "kline")?.note).toContain("แสดงค่าล่าสุดที่ดึงได้");
  });

  it("a retryable failure is retried once, then succeeds", async () => {
    let n = 0;
    const flaky: McpLike = { callTool: vi.fn(async () => { if (n++ === 0) throw { code: "server_unavailable", retryable: true, message: "timeout" }; return { payload: { symbol: "BTCUSDT", price: "1", time: T0 } }; }) };
    const src = new ConnectorSource(async () => flaky, { now: () => T0, retryDelayMs: 0 });
    expect(await src.probe()).toBeNull();
    expect(flaky.callTool).toHaveBeenCalledTimes(2);
  });

  it("answers wrapped in {result} or plain text parse the same", async () => {
    for (const wrap of ["result", "text"] as const) {
      const { src } = setup({}, wrap);
      const q = await src.quote(GOLD);
      expect(q.price).toBeGreaterThan(2000);
      expect(q.freshness.kind).toBe("live");
    }
  });
});
