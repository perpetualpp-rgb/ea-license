import { describe, expect, it, vi, afterEach } from "vitest";
import { ApiSource, freshnessOf, mapBars, mapDetails, mapInstrument, mapObservations, mapQuote, type ApiBars, type ApiDetails, type ApiObservations, type ApiQuote } from "../data/api";
import instrumentsFx from "./fixtures/instruments.json";
import barsFx from "./fixtures/bars.json";
import specRaw from "../../../server/openapi.json?raw";
// recorded from the running API by server/tests/test_frontend_contract.py (fails there if they drift)
import serverInstruments from "./fixtures/server-instruments.json";
import serverBars from "./fixtures/server-bars-5m.json";
import serverQuote from "./fixtures/server-quote.json";
import serverDetails from "./fixtures/server-details.json";
import serverObservations from "./fixtures/server-observations.json";

const spec = JSON.parse(specRaw);
const required = (name: string): string[] => spec.components.schemas[name].required;

describe("fixtures follow server/openapi.json", () => {
  it("have every required field", () => {
    for (const k of required("InstrumentList")) expect(instrumentsFx).toHaveProperty(k);
    for (const i of instrumentsFx.items) for (const k of required("Instrument")) expect(i).toHaveProperty(k);
    for (const k of required("BarsResponse")) expect(barsFx).toHaveProperty(k);
    for (const b of barsFx.bars) for (const k of required("Bar")) expect(b).toHaveProperty(k);
    for (const k of required("Access")) expect(barsFx.access).toHaveProperty(k);
  });
  it("timeframes the app asks for exist in the API enum", () => {
    const tfs = spec.paths["/v1/instruments/{instrument_id}/bars"].get.parameters.find((p: { name: string }) => p.name === "tf").schema.enum;
    for (const tf of ["1m", "5m", "15m", "1h"]) expect(tfs).toContain(tf);
  });
});

describe("mapInstrument", () => {
  it("maps ids, units and decimals; keeps unsupported markets out", () => {
    const [lin, inv, fut] = instrumentsFx.items.map((r) => mapInstrument(r as never));
    expect(lin).toMatchObject({ id: "1", market: "perpetual", contract: "linear", qtyUnit: "BTC", priceDecimals: 1 });
    expect(lin?.caution).toBeUndefined();
    expect(inv).toMatchObject({ id: "2", contract: "inverse", qtyUnit: "สัญญา (100 USD)" });
    expect(inv?.caution).toContain("ยังไม่ได้ตรวจ");
    expect(fut).toBeNull();
  });
});

describe("mapBars", () => {
  const now = Date.parse("2026-10-18T08:47:00Z");
  it("parses decimal strings and ISO time; drops price holes instead of zero-filling", () => {
    const s = mapBars(barsFx as ApiBars, "5m", now);
    expect(s.bars).toHaveLength(2);
    expect(s.bars[0]).toEqual({ t: Date.parse("2026-10-18T08:30:00Z") / 1000, o: 67000.1, h: 67050, l: 66990, c: 67040.2, v: 12.5, buyV: 7 });
    expect(s.bars[1].buyV).toBeNull(); // no aggressor split -> CVD must show as unavailable, not 0
    expect(s.freshness).toEqual({ kind: "live", asOf: Date.parse("2026-10-18T08:40:00Z") });
    expect(s.vwapMethod).toContain("ของแท่ง 5m เริ่มจากแท่งแรกของช่วงที่เลือก");
  });
  it("never says live when delayed, stale, gapped or empty", () => {
    const fx = barsFx as ApiBars;
    expect(mapBars({ ...fx, access: { ...fx.access, level: "delayed", delay_minutes: 15 } }, "5m", now).freshness).toMatchObject({ kind: "delayed", delayMin: 15 });
    expect(mapBars(fx, "5m", now + 3600e3).freshness.kind).toBe("stale");
    const gap = { stream: "kline", reason: "disconnected", status: "open" as const, gap_start: "2026-10-18T08:36:00Z", gap_end: null };
    expect(mapBars({ ...fx, gaps: [gap] }, "5m", now).freshness.kind).toBe("approximate");
    expect(mapBars({ ...fx, gaps: [{ ...gap, status: "recovered" }] }, "5m", now).freshness.kind).toBe("live");
    expect(mapBars({ ...fx, bars: [] }, "5m", now).freshness).toEqual({ kind: "unavailable", asOf: null });
  });
});

describe("ApiSource requests", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("calls only the Flow Lab API with the bearer token and the contract's query names", async () => {
    const calls: { url: string; auth?: string }[] = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      calls.push({ url, auth: (init.headers as Record<string, string>).Authorization });
      return new Response(JSON.stringify(url.includes("/bars") ? barsFx : instrumentsFx), { status: 200 });
    });
    const src = new ApiSource("https://api.flowlab.test/", () => "tok", () => Date.parse("2026-10-18T08:47:00Z"));
    expect((await src.instruments()).map((i) => i.id)).toEqual(["1", "2"]);
    await src.series("1", "5m", "6h");
    const u = new URL(calls[1].url);
    expect(u.origin + u.pathname).toBe("https://api.flowlab.test/v1/instruments/1/bars");
    expect(Object.fromEntries(u.searchParams)).toEqual({ tf: "5m", from: "2026-10-18T02:47:00.000Z", to: "2026-10-18T08:47:00.000Z", source: "kline" });
    expect(calls.every((c) => c.auth === "Bearer tok" && c.url.startsWith("https://api.flowlab.test/v1/"))).toBe(true);
  });
  it("maps HTTP errors to user-facing codes and refuses without a URL", async () => {
    await expect(new ApiSource(null).instruments()).rejects.toMatchObject({ code: "not_configured" });
    for (const [status, code] of [[401, "unauthorized"], [429, "rate_limited"], [404, "not_supported"], [500, "server"]] as const) {
      vi.stubGlobal("fetch", async () => new Response("{}", { status }));
      await expect(new ApiSource("https://api.flowlab.test").instruments()).rejects.toMatchObject({ code });
    }
  });
});

describe("responses recorded from the real API", () => {
  it("instrument list maps to a usable instrument", () => {
    const [i] = serverInstruments.items.map((r) => mapInstrument(r as never));
    expect(i).toMatchObject({ id: "1", symbol: "BTCTESTUSDT", market: "perpetual", contract: "linear", qtyUnit: "BTC", priceDecimals: 2 });
    expect(i?.caution).toContain("ยังไม่ได้ตรวจ"); // spec_status unverified
  });
  it("bars keep the partial last bucket, the no-aggressor bar and the open gap visible", () => {
    const s = mapBars(serverBars as ApiBars, "5m", Date.parse("2026-01-05T10:47:00Z"));
    expect(s.bars).toHaveLength(serverBars.bars.length);
    expect(s.bars.map((b) => b.v).reduce((a, b) => a + b, 0)).toBeCloseTo(serverBars.bars.reduce((a, b) => a + Number(b.volume), 0), 9);
    expect(s.bars.filter((b) => b.buyV === null)).toHaveLength(1);
    expect(s.freshness.kind).toBe("approximate"); // open kline gap in range
    expect(s.freshness.asOf).toBe(Date.parse("2026-01-05T10:47:00Z")); // forming bucket: data only up to `to`, not 10:50
  });
});

describe("market mapping rules", () => {
  const rt = { level: "realtime" as const, delay_minutes: 0, data_until: null, valid_until: null };
  const now = Date.parse("2026-10-18T09:00:00Z");
  it("freshness: live only for realtime data from the last two minutes", () => {
    expect(freshnessOf(rt, now - 30_000, now).kind).toBe("live");
    expect(freshnessOf(rt, now - 121_000, now).kind).toBe("stale");
    expect(freshnessOf({ ...rt, level: "delayed", delay_minutes: 15 }, now - 900_000, now)).toMatchObject({ kind: "delayed", delayMin: 15 });
    expect(freshnessOf(rt, null, now).kind).toBe("unavailable");
  });
  it("quote keeps a missing 24h change as null instead of a shorter window", () => {
    const q = mapQuote("1", { instrument_id: 1, access: rt, last_trade: { price: "81700.03", at: "2026-10-18T08:59:50Z", source: "trade" }, mark: null, change_24h: null,
      spark: [{ t: "2026-10-18T08:30:00Z", close: "81650" }] }, now);
    expect(q).toMatchObject({ price: 81700.03, changePct: null, priceType: "last_trade", spark: [81650] });
    expect(q.freshness.kind).toBe("live");
  });
  it("observations are worded with units and window, and unknown kinds are skipped", () => {
    const ins = mapInstrument(instrumentsFx.items[0] as never)!;
    const obs = mapObservations("1", ins, { instrument_id: 1, access: rt, at: "2026-10-18T09:00:00Z", items: [
      { kind: "cvd", at: "2026-10-18T09:00:00Z", window_s: 300, evidence: "chart", values: { cvd_change: "-12.5", price_change_pct: "0.12" }, units: { cvd_change: "BTC" } },
      { kind: "funding", at: "2026-10-18T09:00:00Z", window_s: null, evidence: "oi", values: { rate_pct: "0.0100", kind: "estimated" }, units: {} },
      { kind: "mystery" as never, at: "2026-10-18T09:00:00Z", window_s: null, evidence: "chart", values: {}, units: {} },
    ] });
    expect(obs.map((o) => o.title)).toEqual(["CVD ลดลง", "Funding (ประมาณการ)"]);
    expect(obs[0].measured).toBe("CVD −12.50 BTC ใน 5 นาที · ราคา +0.12%");
    expect(obs[1].measured).toBe("+0.0100% ต่อรอบ");
  });
});

describe("market endpoints recorded from the real API", () => {
  const at = Date.parse("2026-01-05T10:47:00Z");
  it("quote: last trade, 24h change and spark map to numbers", () => {
    const q = mapQuote("1", serverQuote as ApiQuote, at);
    expect(q.price).toBe(Number(serverQuote.last_trade.price));
    expect(q.changePct).toBe(Number(serverQuote.change_24h.pct));
    expect(q.spark).toHaveLength(serverQuote.spark.length);
    expect(q.spark.every(Number.isFinite)).toBe(true);
    expect(q.freshness.kind).toBe("live"); // last trade 30 s before `at`
  });
  it("details: walls, wall events, big trades, OI, funding, profile, prices and quality", () => {
    const d = mapDetails(serverDetails as ApiDetails);
    expect(d.walls).toHaveLength(serverDetails.book.walls.length);
    expect(d.walls.every((w) => Number.isFinite(w.price) && Number.isFinite(w.size) && w.ageSec >= 0)).toBe(true);
    expect(new Set(d.wallEvents.map((e) => e.outcome))).toEqual(new Set(serverDetails.book.events.map((e) => e.outcome)));
    expect(d.bigTrades).toHaveLength(serverDetails.big_trades.items.length);
    expect(d.bigMethod).toBe(serverDetails.big_trades.method);
    expect(d.oi).toMatchObject({ value: Number(serverDetails.oi.value), unit: serverDetails.oi.unit, change5mPct: Number(serverDetails.oi.change_5m_pct) });
    expect(d.oi?.funding).toMatchObject({ ratePct: Number(serverDetails.funding.rate_pct), kind: "estimated", nextAt: Date.parse(serverDetails.funding.next_at) });
    expect(d.profile?.poc).toBe(Number(serverDetails.profile.poc));
    expect(d.prices.map((p) => p.type)).toEqual(["last_trade", "mark", "index"]);
    expect(d.quality.map((q) => q.stream).sort()).toEqual(["book", "kline", "liquidations", "mark", "oi", "trades"]);
  });
  it("observations: every recorded kind is worded, values come through unchanged", () => {
    const ins = mapInstrument(serverInstruments.items[0] as never)!;
    const obs = mapObservations("1", ins, serverObservations as unknown as ApiObservations);
    expect(obs.map((o) => o.kind)).toEqual(serverObservations.items.map((o) => o.kind));
    expect(obs.every((o) => o.title && o.measured && o.explain && !/NaN|undefined/.test(o.measured))).toBe(true);
    const oi = serverObservations.items.find((o) => o.kind === "oi")!;
    expect(obs.find((o) => o.kind === "oi")!.measured).toContain(`${Number(oi.values.oi_change_pct).toFixed(2)}%`);
  });
});
