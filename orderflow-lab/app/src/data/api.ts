// Production adapter: talks only to the Flow Lab API (server/), never to exchanges or
// claude.ai connectors from the member's browser. Endpoints follow server/openapi.json.
// Without a server URL every call rejects with "not_configured".
import type {
  AlertEvent, AlertMetric, Bar, DataKind, EvidenceTab, Instrument, Lookback, MarketDetails, Observation, ObservationKind, OiInfo, PriceType,
  Quote, Series, StreamName, StreamQuality, Timeframe, WallEvent, WallRow,
} from "./types";
import { SourceError, type MarketSource } from "./source";

const LOOKBACK_MS: Record<Lookback, number> = { "6h": 6 * 3600e3, "24h": 86400e3, "3d": 3 * 86400e3 };
const TF_SEC: Record<Timeframe, number> = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600 };

// -- wire shapes (server/openapi.json components.schemas) ----------------------------------
export interface ApiAccess { level: "realtime" | "delayed" | "none"; delay_minutes: number; data_until: string | null; valid_until: string | null }
export interface ApiInstrument {
  instrument_id: number; venue: string; symbol: string; market_type: string; contract_type: string;
  base_asset: string; quote_asset: string; settlement_asset: string; contract_multiplier: string;
  multiplier_unit: "base" | "quote"; tick_size: string | null; qty_step: string | null;
  spec_status: "unverified" | "verified"; source_notice?: string;
}
export interface ApiBar {
  t: string; open: string | null; high: string | null; low: string | null; close: string | null;
  volume: string | null; taker_buy_volume: string | null; taker_sell_volume: string | null;
  unknown_side_volume: string | null; delta: string | null; trades: number | null; complete: boolean;
}
export interface ApiGap { stream: string; reason: string; status: "open" | "recovered" | "partial" | "unrecoverable"; gap_start: string | null; gap_end: string | null }
export interface ApiBars {
  instrument_id: number; tf: string; source: string; method: string; access: ApiAccess;
  from: string; to: string; bars: ApiBar[]; gaps: ApiGap[];
}

const num = (s: string | null) => (s === null ? null : Number(s));

function decimals(tick: string | null): number {
  if (!tick) return 2;
  const frac = tick.replace(/0+$/, "").split(".")[1];
  return frac ? frac.length : 0;
}

const VENUE_NAME: Record<string, string> = { binance: "Binance" };

/** null for markets the screens cannot show yet (future, cfd, index) — never guessed into another type */
export function mapInstrument(r: ApiInstrument): Instrument | null {
  if (r.market_type !== "perpetual" && r.market_type !== "spot") return null;
  if (r.contract_type !== "linear" && r.contract_type !== "inverse" && r.contract_type !== "spot") return null;
  const unitAsset = r.multiplier_unit === "quote" ? r.quote_asset : r.base_asset;
  const qtyUnit = Number(r.contract_multiplier) === 1 ? unitAsset : `สัญญา (${r.contract_multiplier} ${unitAsset})`;
  return {
    id: String(r.instrument_id), venue: VENUE_NAME[r.venue] ?? r.venue, symbol: r.symbol, market: r.market_type,
    base: r.base_asset, quote: r.quote_asset, qtyUnit, contract: r.contract_type,
    priceDecimals: decimals(r.tick_size),
    ...(r.spec_status === "unverified" ? { caution: "สเปกสัญญายังไม่ได้ตรวจกับประกาศของตลาด" } : {}),
  };
}

/** Bars with a missing price are dropped (a hole in the chart), never filled with zero. */
export function mapBars(res: ApiBars, tf: Timeframe, nowMs: number): Series {
  const bars: Bar[] = [];
  for (const b of res.bars) {
    const o = num(b.open), h = num(b.high), l = num(b.low), c = num(b.close);
    if (o === null || h === null || l === null || c === null) continue;
    bars.push({ t: Date.parse(b.t) / 1000, o, h, l, c, v: num(b.volume) ?? 0, buyV: num(b.taker_buy_volume) });
  }
  const last = bars.at(-1);
  // a still-forming bucket holds data only up to the response's `to`, not to the bucket's end
  const asOf = last ? Math.min((last.t + TF_SEC[tf]) * 1000, Date.parse(res.to)) : null;
  const openGaps = res.gaps.some((g) => g.status !== "recovered");
  let kind: DataKind;
  if (!last || res.access.level === "none") kind = "unavailable";
  else if (res.access.level === "delayed") kind = "delayed";
  // "live" only when the newest bar actually closed within two bars of now
  else if (nowMs - (asOf as number) > 2 * TF_SEC[tf] * 1000) kind = "stale";
  else kind = openGaps ? "approximate" : "live";
  return {
    bars,
    freshness: { kind, asOf, ...(res.access.level === "delayed" ? { delayMin: res.access.delay_minutes } : {}) },
    cvdAnchor: { label: "เริ่มนับจากแท่งแรกของช่วงที่เลือก (UTC)", startT: bars[0]?.t ?? 0 },
    // VWAP is computed in the app from these bars (domain/indicators.vwap), anchored like CVD
    vwapMethod: `typical price (H+L+C)/3 × volume ของแท่ง ${tf} เริ่มจากแท่งแรกของช่วงที่เลือก (ประมาณจากแท่ง ไม่ใช่ VWAP จากทุกรายการซื้อขาย)`,
  };
}

// -- market endpoints: quote / details / observations ---------------------------------------
export interface ApiQuote {
  instrument_id: number; access: ApiAccess;
  last_trade: { price: string; at: string; source: "trade" | "kline" } | null;
  mark: { price: string | null; index: string | null; at: string } | null;
  change_24h: { pct: string; ref_price: string; ref_at: string } | null;
  spark: { t: string; close: string }[];
}
type StreamStatusWire = StreamQuality["status"];
export interface ApiDetails {
  instrument_id: number; access: ApiAccess; from: string; to: string; units: { price: string; size: string };
  book: null | {
    at: string | null; synced: boolean; method: string;
    walls: { side: "bid" | "ask"; price: string; size: string; mult: string; age_s: number; status: WallRow["status"] }[];
    events: { side: "bid" | "ask"; price: string; size: string; outcome: WallEvent["outcome"]; at: string }[];
  };
  big_trades: { threshold: string | null; method: string; items: { at: string; side: "buy" | "sell"; price: string; size: string }[] };
  profile: null | { bin_size: string; poc: string | null; va_low: string | null; va_high: string | null; method: string };
  oi: null | { value: string | null; unit: string; at: string; change_5m_pct: string | null; change_1h_pct: string | null; series: { t: string; value: string }[] };
  funding: null | { rate_pct: string; kind: "estimated" | "settled"; next_at: string | null; at: string };
  prices: { type: PriceType; value: string | null; at: string | null }[];
  quality: { stream: StreamName; status: StreamStatusWire; last_at: string | null; note: string | null }[];
}
export interface ApiObservation {
  kind: ObservationKind; at: string; window_s: number | null; evidence: EvidenceTab;
  values: Record<string, string>; units: Record<string, string>;
}
export interface ApiObservations { instrument_id: number; access: ApiAccess; at: string; items: ApiObservation[] }
interface ApiEvent {
  event_id: string; rule_id: string | null; instrument_id: number; metric: string; message: string;
  observed_value: string; threshold: string; unit: string; window_start: string; window_end: string;
  observed_at: string; is_backfill: boolean; dedupe_key: string; acknowledged_at: string | null;
}
const SHARED_METRICS: Record<string, AlertMetric | undefined> = { oi_change_pct: "oi_change_pct", cvd_change: "cvd_change", funding_rate: "funding_rate" };

const ms = (s: string | null) => (s === null ? null : Date.parse(s));
const LIVE_WINDOW_MS = 120_000;

/** "live" only for realtime access with an exchange time inside the last two minutes */
export function freshnessOf(access: ApiAccess, asOf: number | null, nowMs: number): Series["freshness"] {
  if (asOf === null || access.level === "none") return { kind: "unavailable", asOf };
  if (access.level === "delayed") return { kind: "delayed", asOf, delayMin: access.delay_minutes };
  return { kind: nowMs - asOf <= LIVE_WINDOW_MS ? "live" : "stale", asOf };
}

export function mapQuote(id: string, r: ApiQuote, nowMs: number): Quote {
  const at = r.last_trade ? Date.parse(r.last_trade.at) : null;
  return {
    instrumentId: id, price: r.last_trade ? Number(r.last_trade.price) : null, priceType: "last_trade",
    changePct: r.change_24h ? Number(r.change_24h.pct) : null, changeWindow: "24h",
    freshness: freshnessOf(r.access, at, nowMs), spark: r.spark.map((p) => Number(p.close)),
  };
}

export function mapDetails(r: ApiDetails): MarketDetails {
  const oi: OiInfo | null = r.oi || r.funding ? {
    value: r.oi ? num(r.oi.value) : null, unit: r.oi?.unit ?? r.units.size,
    change5mPct: r.oi ? num(r.oi.change_5m_pct) : null, change1hPct: r.oi ? num(r.oi.change_1h_pct) : null,
    funding: r.funding ? { ratePct: Number(r.funding.rate_pct), kind: r.funding.kind, nextAt: ms(r.funding.next_at) } : null,
    series: (r.oi?.series ?? []).map((p) => ({ t: Date.parse(p.t), v: Number(p.value) })),
  } : null;
  return {
    walls: (r.book?.walls ?? []).map((w) => ({ side: w.side, price: Number(w.price), size: Number(w.size), ageSec: w.age_s, status: w.status })),
    wallEvents: (r.book?.events ?? []).map((e) => ({ side: e.side, price: Number(e.price), size: Number(e.size), outcome: e.outcome, at: Date.parse(e.at) })),
    wallMethod: r.book?.method ?? "ยังไม่มี order book ในช่วงนี้",
    bigTrades: r.big_trades.items.map((t) => ({ at: Date.parse(t.at), side: t.side, price: Number(t.price), size: Number(t.size) })),
    bigMethod: r.big_trades.method,
    oi,
    quality: r.quality.map((q) => ({ stream: q.stream, status: q.status, lastAt: ms(q.last_at), ...(q.note ? { note: q.note } : {}) })),
    profile: r.profile ? { binSize: Number(r.profile.bin_size), poc: num(r.profile.poc), vaLow: num(r.profile.va_low), vaHigh: num(r.profile.va_high), method: r.profile.method } : null,
    prices: r.prices.map((p) => ({ type: p.type, value: num(p.value) })),
  };
}

const signed = (v: number, d: number) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d })}`;
const plain = (v: number, d: number) => v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const minutes = (s: number | null) => (s === null ? "" : s % 3600 === 0 ? `${s / 3600} ชั่วโมง` : `${Math.round(s / 60)} นาที`);
const sizeDp = (v: number) => (Math.abs(v) >= 100 ? 0 : Math.abs(v) >= 1 ? 2 : 4);

/** The server sends measured values; the wording (Thai, units, window) is made here. */
export function mapObservations(id: string, ins: Instrument | undefined, r: ApiObservations): Observation[] {
  const qty = ins?.qtyUnit ?? "";
  const quote = ins?.quote ?? "";
  const dp = ins?.priceDecimals ?? 2;
  const out: Observation[] = [];
  for (const o of r.items) {
    const v = (k: string) => Number(o.values[k]);
    const base = { id: `${id}-${o.kind}-${o.at}`, instrumentId: id, kind: o.kind, at: Date.parse(o.at), evidence: o.evidence };
    const win = minutes(o.window_s);
    switch (o.kind) {
      case "cvd": {
        const d = v("cvd_change"), unit = o.units.cvd_change ?? qty;
        const px = o.values.price_change_pct !== undefined ? ` · ราคา ${signed(v("price_change_pct"), 2)}%` : "";
        out.push({ ...base, title: d >= 0 ? "CVD เพิ่มขึ้น" : "CVD ลดลง", measured: `CVD ${signed(d, sizeDp(d))} ${unit} ใน ${win}${px}`,
          explain: d >= 0 ? "ปริมาณที่ผู้ซื้อกดซื้อทันที มากกว่าที่ผู้ขายกดขายทันที" : "ปริมาณที่ผู้ขายกดขายทันที มากกว่าที่ผู้ซื้อกดซื้อทันที" });
        break;
      }
      case "oi": {
        const d = v("oi_change_pct");
        const px = o.values.price_change_pct !== undefined ? ` · ราคา ${signed(v("price_change_pct"), 2)}%` : "";
        out.push({ ...base, title: d >= 0 ? "OI เพิ่มขึ้น" : "OI ลดลง", measured: `OI ${signed(d, 2)}% ใน ${win}${px}`,
          explain: "สถานะคงค้างเปลี่ยน ทุกสัญญามีทั้งฝั่ง Long และ Short จึงไม่บอกว่าฝั่งไหนมากกว่า" });
        break;
      }
      case "wall": {
        const side = o.values.side === "bid" ? "BID" : "ASK";
        const size = v("size");
        out.push({ ...base, title: `กำแพง ${side}`,
          measured: `${plain(size, sizeDp(size))} ${o.units.size ?? qty} ที่ ${plain(v("price"), dp)} ${o.units.price ?? quote} · ${plain(v("mult"), 1)} เท่าของค่ากลาง · เห็นมา ${minutes(Math.round(v("age_s") / 60) * 60) || "ไม่ถึง 1 นาที"}`,
          explain: "คำสั่งรอซื้อ/ขายที่ใหญ่ผิดปกติเทียบกับระดับใกล้เคียง ยืนยันไม่ได้ว่าจะถูกจับคู่หรือถูกถอน" });
        break;
      }
      case "bigtrade": {
        const net = v("net_size"), unit = o.units.net_size ?? qty;
        out.push({ ...base, title: `ไม้ใหญ่ ${win}`,
          measured: `ซื้อ ${v("buy_count")} : ขาย ${v("sell_count")} ไม้ · สุทธิ ${signed(net, sizeDp(net))} ${unit} (ไม้ ≥ ${plain(v("threshold"), sizeDp(v("threshold")))} ${unit})`,
          explain: "นับจากฝั่งที่กดซื้อหรือกดขายทันที 1 รายการอาจรวมหลายคำสั่ง" });
        break;
      }
      case "funding": {
        const est = o.values.kind !== "settled";
        out.push({ ...base, title: est ? "Funding (ประมาณการ)" : "Funding ที่จ่ายแล้ว", measured: `${signed(v("rate_pct"), 4)}% ต่อรอบ`,
          explain: "ค่าบวก = ฝั่ง Long จ่ายให้ Short, ค่าลบ = Short จ่ายให้ Long ไม่ได้บอกทิศทางราคา" });
        break;
      }
    }
  }
  return out;
}

export class ApiSource implements MarketSource {
  readonly mode = "api" as const;
  constructor(private baseUrl: string | null, private token: () => string | null = () => null, private now: () => number = Date.now) {}

  private async get<T>(path: string): Promise<T> {
    if (!this.baseUrl) throw new SourceError("not_configured", "no API base URL");
    const token = this.token();
    let res: Response;
    try {
      res = await fetch(this.baseUrl.replace(/\/$/, "") + path, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
    } catch {
      throw new SourceError("network", path);
    }
    if (res.status === 401 || res.status === 403) throw new SourceError("unauthorized", path);
    if (res.status === 429) throw new SourceError("rate_limited", path);
    if (res.status === 404) throw new SourceError("not_supported", path);
    if (!res.ok) throw new SourceError("server", `${res.status} ${path}`);
    return res.json() as Promise<T>;
  }

  async instruments() {
    const res = await this.get<{ access: ApiAccess; items: ApiInstrument[] }>("/v1/instruments");
    return res.items.map(mapInstrument).filter((i): i is Instrument => i !== null);
  }

  async series(id: string, tf: Timeframe, lookback: Lookback): Promise<Series> {
    const to = this.now();
    const from = to - LOOKBACK_MS[lookback];
    const q = new URLSearchParams({ tf, from: new Date(from).toISOString(), to: new Date(to).toISOString(), source: "kline" });
    const res = await this.get<ApiBars>(`/v1/instruments/${encodeURIComponent(id)}/bars?${q}`);
    return mapBars(res, tf, to);
  }

  async quote(id: string): Promise<Quote> {
    const res = await this.get<ApiQuote>(`/v1/instruments/${encodeURIComponent(id)}/quote`);
    return mapQuote(id, res, this.now());
  }

  async observations(id: string): Promise<Observation[]> {
    const [ins, res] = await Promise.all([this.instrument(id), this.get<ApiObservations>(`/v1/instruments/${encodeURIComponent(id)}/observations`)]);
    return mapObservations(id, ins, res);
  }

  async details(id: string, _tf: Timeframe, lookback: Lookback): Promise<MarketDetails> {
    const to = this.now();
    const q = new URLSearchParams({ from: new Date(to - LOOKBACK_MS[lookback]).toISOString(), to: new Date(to).toISOString() });
    return mapDetails(await this.get<ApiDetails>(`/v1/instruments/${encodeURIComponent(id)}/details?${q}`));
  }

  /** Server alert events whose metric the app's rule screen knows; other metrics are listed in the API only. */
  async alertHistory(): Promise<AlertEvent[]> {
    const res = await this.get<{ items: ApiEvent[] }>("/v1/alerts/events?limit=50");
    return res.items.flatMap((e) => {
      const metric = SHARED_METRICS[e.metric];
      if (!metric) return [];
      return [{
        id: e.event_id, ruleId: e.rule_id, instrumentId: String(e.instrument_id), metric, title: e.message,
        measured: Number(e.observed_value), threshold: Number(e.threshold), unit: e.unit,
        window: `${Math.round((Date.parse(e.window_end) - Date.parse(e.window_start)) / 60_000)}m`,
        at: Date.parse(e.observed_at), live: !e.is_backfill, dedupeKey: e.dedupe_key, ack: e.acknowledged_at !== null,
      }];
    });
  }

  private instrumentCache: Promise<Instrument[]> | null = null;
  private async instrument(id: string): Promise<Instrument | undefined> {
    this.instrumentCache ??= this.instruments().catch((e) => { this.instrumentCache = null; throw e; });
    return (await this.instrumentCache).find((i) => i.id === id);
  }
}
