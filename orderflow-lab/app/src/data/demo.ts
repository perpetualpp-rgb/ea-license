// Demo adapter: deterministic, clearly labelled fake data so the screens can be built and
// tested before any licensed data source exists. Nothing here is a real price.
import { aggregate, cvd, profile, sessionStart, vwap } from "../domain/indicators";
import type {
  AlertEvent, Bar, Instrument, Lookback, MarketDetails, Observation, Quote, Series, StreamQuality, Timeframe,
} from "./types";
import type { MarketSource } from "./source";

export const DEMO_INSTRUMENTS: Instrument[] = [
  { id: "binance-um-btcusdt", venue: "Binance", symbol: "BTCUSDT", market: "perpetual", base: "BTC", quote: "USDT", qtyUnit: "BTC", contract: "linear", priceDecimals: 2 },
  { id: "binance-um-xauusdt", venue: "Binance", symbol: "XAUUSDT", market: "perpetual", base: "XAU", quote: "USDT", qtyUnit: "oz", contract: "linear", priceDecimals: 2,
    caution: "ไม่ใช่ COMEX GC และไม่ใช่ XAUUSD ของโบรกเกอร์" },
  { id: "binance-spot-btcusdt", venue: "Binance", symbol: "BTCUSDT", market: "spot", base: "BTC", quote: "USDT", qtyUnit: "BTC", contract: "spot", priceDecimals: 2 },
];

const PARAMS: Record<string, { start: number; vol: number; qty: number; bin: number; wallStep: number }> = {
  "binance-um-btcusdt": { start: 66800, vol: 22, qty: 60, bin: 25, wallStep: 50 },
  "binance-um-xauusdt": { start: 2350, vol: 0.55, qty: 300, bin: 0.5, wallStep: 1 },
  "binance-spot-btcusdt": { start: 66790, vol: 22, qty: 25, bin: 25, wallStep: 50 },
};

const TF_SEC: Record<Timeframe, number> = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600 };
const LOOKBACK_SEC: Record<Lookback, number> = { "6h": 6 * 3600, "24h": 86400, "3d": 3 * 86400 };

function rng(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const hash = (s: string) => [...s].reduce((h, c) => (Math.imul(h, 31) + c.charCodeAt(0)) >>> 0, 7);

/** 3 days of 1-minute bars ending at `nowMs`, the same for every call within that minute */
export function demoMinuteBars(id: string, nowMs: number): Bar[] {
  const p = PARAMS[id];
  const end = Math.floor(nowMs / 60000) * 60;
  const n = 3 * 1440;
  const r = rng(hash(id) ^ Math.floor(end / 86400)); // new path each UTC day, stable within it
  const out: Bar[] = [];
  let price = p.start;
  let drift = 0;
  for (let i = 0; i < n; i++) {
    drift = drift * 0.97 + (r() - 0.5) * 0.15;
    const o = price;
    const c = Math.max(p.start * 0.5, o + (r() - 0.5 + drift) * p.vol);
    const h = Math.max(o, c) + r() * p.vol * 0.6;
    const l = Math.min(o, c) - r() * p.vol * 0.6;
    const v = p.qty * (0.4 + r() * 1.6 + (r() < 0.03 ? 4 : 0));
    const lean = Math.max(0.15, Math.min(0.85, 0.5 + (c - o) / (p.vol * 4) + (r() - 0.5) * 0.2));
    out.push({ t: end - (n - 1 - i) * 60, o, h, l, c, v, buyV: v * lean });
    price = c;
  }
  return out;
}

function demoQuality(id: string, now: number): StreamQuality[] {
  const ok = (stream: StreamQuality["stream"]): StreamQuality => ({ stream, status: "ok", lastAt: now - 1500 });
  if (id === "binance-um-xauusdt")
    return [ok("trades"), { stream: "book", status: "book_resync", lastAt: now - 42_000, note: "ลำดับ update ของ book ขาด กำลังขอ snapshot ใหม่ — พักการแจ้งเตือนที่ใช้ book" },
      ok("kline"), ok("oi"), ok("mark"), ok("liquidations")];
  if (id === "binance-spot-btcusdt")
    return [ok("trades"), ok("book"), ok("kline"), { stream: "oi", status: "unavailable", lastAt: null, note: "ตลาด spot ไม่มี Open Interest" },
      { stream: "mark", status: "unavailable", lastAt: null, note: "ตลาด spot ไม่มี mark price" }, { stream: "liquidations", status: "unavailable", lastAt: null, note: "ตลาด spot ไม่มี liquidation" }];
  return [ok("trades"), ok("book"), ok("kline"), ok("oi"), ok("mark"), ok("liquidations")];
}

function demoOi(id: string, now: number) {
  if (id === "binance-spot-btcusdt") return null;
  const r = rng(hash(id + "oi"));
  const unit = id.includes("xau") ? "oz" : "BTC";
  let v = id.includes("xau") ? 131000 : 82000;
  const series: { t: number; v: number }[] = [];
  const end = Math.floor(now / 300000) * 300;
  for (let i = 287; i >= 0; i--) {
    v *= 1 + (r() - 0.48) * 0.004;
    series.push({ t: end - i * 300, v });
  }
  series[series.length - 1].v = series[series.length - 2].v * 1.021; // a visible 5-minute jump for the demo
  const last = series[series.length - 1].v;
  const pct = (back: number) => (last / series[series.length - 1 - back].v - 1) * 100;
  return { value: last, unit, change5mPct: pct(1), change1hPct: pct(12), funding: { ratePct: 0.0083, kind: "estimated" as const, nextAt: (Math.floor(now / 28_800_000) + 1) * 28_800_000 }, series };
}

export class DemoSource implements MarketSource {
  readonly mode = "demo" as const;
  constructor(private clock: () => number = () => Date.now()) {}

  async instruments() {
    return DEMO_INSTRUMENTS;
  }

  private minute(id: string) {
    return demoMinuteBars(id, this.clock());
  }

  async quote(id: string): Promise<Quote> {
    const bars = this.minute(id);
    const last = bars[bars.length - 1];
    const dayAgo = bars[bars.length - 1 - 1440];
    const spark = bars.slice(-1440).filter((_, i) => i % 30 === 0).map((b) => b.c);
    return { instrumentId: id, price: last.c, priceType: "last_trade", changePct: (last.c / dayAgo.c - 1) * 100, changeWindow: "24h",
      freshness: { kind: "demo", asOf: last.t * 1000 + 59_000 }, spark };
  }

  async series(id: string, tf: Timeframe, lookback: Lookback): Promise<Series> {
    const all = this.minute(id);
    const nowS = all[all.length - 1].t + 60;
    const from = nowS - LOOKBACK_SEC[lookback];
    const bars = aggregate(all.filter((b) => b.t >= from), TF_SEC[tf]);
    const anchor = sessionStart(nowS);
    return { bars, freshness: { kind: "demo", asOf: nowS * 1000 - 1000 }, cvdAnchor: { label: "เริ่มนับ 07:00 UTC+7", startT: anchor < from ? from : anchor },
      vwapMethod: "typical price × volume ของแท่ง 1 นาที ตั้งแต่ 07:00 UTC+7 (ประมาณจากแท่ง ไม่ใช่ VWAP จากทุกรายการซื้อขาย)" };
  }

  async observations(id: string): Promise<Observation[]> {
    const ins = DEMO_INSTRUMENTS.find((x) => x.id === id)!;
    const bars = this.minute(id);
    const last5 = bars.slice(-5);
    const d5 = last5.reduce((a, b) => a + (b.buyV! - (b.v - b.buyV!)), 0);
    const now = this.clock();
    const out: Observation[] = [{
      id: `${id}-cvd`, instrumentId: id, kind: "cvd", at: now - 20_000, evidence: "chart",
      title: d5 >= 0 ? "CVD เพิ่มขึ้น" : "CVD ลดลง",
      measured: `CVD ${d5 >= 0 ? "+" : "−"}${Math.abs(d5).toFixed(ins.qtyUnit === "oz" ? 0 : 1)} ${ins.qtyUnit} ใน 5 นาที`,
      explain: d5 >= 0 ? "ปริมาณที่ผู้ซื้อกดซื้อทันที มากกว่าที่ผู้ขายกดขายทันที" : "ปริมาณที่ผู้ขายกดขายทันที มากกว่าที่ผู้ซื้อกดซื้อทันที",
    }];
    const oi = demoOi(id, now);
    if (oi) out.push({ id: `${id}-oi`, instrumentId: id, kind: "oi", at: now - 60_000, evidence: "oi", title: "OI เพิ่มขึ้น",
      measured: `OI +${oi.change5mPct!.toFixed(1)}% ใน 5 นาที (หน่วย ${oi.unit})`, explain: "สถานะคงค้างเพิ่ม ทุกสัญญามีทั้งฝั่ง Long และ Short จึงไม่บอกว่าฝั่งไหนมากกว่า" });
    const w = this.walls(id)[0];
    if (w) out.push({ id: `${id}-wall`, instrumentId: id, kind: "wall", at: now - 95_000, evidence: "walls", title: `กำแพง ${w.side === "bid" ? "BID" : "ASK"}`,
      measured: `${w.size.toFixed(1)} ${ins.qtyUnit} ที่ ${w.price.toLocaleString("en-US")} ${ins.quote} · อยู่มา ${Math.round(w.ageSec / 60)} นาที`,
      explain: "คำสั่งรอซื้อ/ขายที่ใหญ่ผิดปกติเทียบกับระดับใกล้เคียง ยืนยันไม่ได้ว่าจะถูกจับคู่หรือถูกถอน" });
    return out.slice(0, 3);
  }

  private walls(id: string) {
    const p = PARAMS[id];
    const last = this.minute(id).slice(-1)[0].c;
    const r = rng(hash(id + "walls") ^ Math.floor(this.clock() / 600000));
    const base = Math.round(last / p.wallStep) * p.wallStep;
    return [
      { side: "bid" as const, price: base - 3 * p.wallStep, size: p.qty * (4 + r() * 3), ageSec: 300 + Math.round(r() * 1500), status: "holding" as const },
      { side: "ask" as const, price: base + 4 * p.wallStep, size: p.qty * (3 + r() * 3), ageSec: 120 + Math.round(r() * 900), status: "adding" as const },
      { side: "bid" as const, price: base - 6 * p.wallStep, size: p.qty * (2 + r() * 2), ageSec: 40, status: "new" as const },
    ].sort((a, z) => z.size - a.size);
  }

  async details(id: string, tf: Timeframe, lookback: Lookback): Promise<MarketDetails> {
    const now = this.clock();
    const p = PARAMS[id];
    const s = await this.series(id, tf, lookback);
    const all = this.minute(id).filter((b) => b.t >= s.bars[0].t);
    const prof = profile(all, p.bin);
    const last = s.bars[s.bars.length - 1].c;
    const r = rng(hash(id + "bt") ^ Math.floor(now / 600000));
    const bigTrades = Array.from({ length: 6 }, (_, i) => ({ at: now - (i + 1) * 170_000, side: (r() < 0.5 ? "buy" : "sell") as "buy" | "sell", price: last + (r() - 0.5) * p.vol * 6, size: p.qty * (0.5 + r()) }));
    const oi = demoOi(id, now);
    const spot = id.includes("spot");
    const quality = demoQuality(id, now);
    const bookOk = quality.find((q) => q.stream === "book")?.status === "ok";
    return {
      walls: bookOk ? this.walls(id) : [],
      wallEvents: [
        { side: "ask", price: Math.round(last / p.wallStep + 2) * p.wallStep, size: p.qty * 3.2, outcome: "price_reached", at: now - 900_000 },
        { side: "bid", price: Math.round(last / p.wallStep - 5) * p.wallStep, size: p.qty * 2.6, outcome: "removed", at: now - 1_500_000 },
      ],
      wallMethod: "ขนาดใน book ≥ 4 เท่าของค่ามัธยฐาน 200 ระดับใกล้ราคา ภายใน ±1% · ต้องเห็นอย่างน้อย 2 รอบ",
      bigTrades,
      bigMethod: `ไม้ ≥ เปอร์เซ็นไทล์ที่ 99 ของ 4 ชั่วโมงก่อนหน้า (ไม่ใช้ข้อมูลอนาคต) · 1 aggTrade อาจรวมหลายคำสั่ง`,
      oi,
      quality,
      profile: { binSize: p.bin, poc: prof.poc, vaLow: prof.vaLow, vaHigh: prof.vaHigh, method: "กระจาย volume ของแท่ง 1 นาทีตามช่วง high–low (ประมาณ ไม่ใช่ volume ที่ราคาจริง) · value area 70%" },
      prices: spot ? [{ type: "last_trade", value: last }, { type: "mark", value: null }, { type: "index", value: null }]
        : [{ type: "last_trade", value: last }, { type: "mark", value: last + p.vol * 0.05 }, { type: "index", value: last - p.vol * 0.08 }],
    };
  }

  async alertHistory(): Promise<AlertEvent[]> {
    const now = this.clock();
    return [
      { id: "demo-ev-1", ruleId: null, instrumentId: "binance-um-btcusdt", metric: "wall_size", title: "BTCUSDT · กำแพงลดลง", measured: 35, threshold: 30, unit: "% ใน 30 วินาที", window: "30s",
        at: now - 18 * 60_000, live: true, dedupeKey: "demo:wall:1", ack: false },
      { id: "demo-ev-2", ruleId: null, instrumentId: "binance-um-btcusdt", metric: "oi_change_pct", title: "BTCUSDT · OI เปลี่ยนแปลง", measured: 2.1, threshold: 2, unit: "% / 5 นาที", window: "5m",
        at: now - 22 * 60_000, live: true, dedupeKey: "demo:oi:2", ack: false },
      { id: "demo-ev-3", ruleId: null, instrumentId: "binance-um-xauusdt", metric: "cvd_change", title: "XAUUSDT · CVD เปลี่ยนเร็ว", measured: 1840, threshold: 1500, unit: "oz / 5 นาที", window: "5m",
        at: now - 3 * 3_600_000, live: false, dedupeKey: "demo:cvd:3", ack: true },
    ];
  }
}

export { TF_SEC, LOOKBACK_SEC, cvd, vwap };
