// Production adapter: talks only to the Flow Lab API (server/), never to exchanges or
// claude.ai connectors from the member's browser. Endpoints follow server/openapi.json.
// Until a server URL and sign-in exist, every call rejects with "not_configured".
import type { AlertEvent, Bar, DataKind, Instrument, Lookback, MarketDetails, Observation, Quote, Series, Timeframe } from "./types";
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

/** null for markets the screens cannot show yet (future, cfd, index) — never guessed into another type */
export function mapInstrument(r: ApiInstrument): Instrument | null {
  if (r.market_type !== "perpetual" && r.market_type !== "spot") return null;
  if (r.contract_type !== "linear" && r.contract_type !== "inverse" && r.contract_type !== "spot") return null;
  const unitAsset = r.multiplier_unit === "quote" ? r.quote_asset : r.base_asset;
  const qtyUnit = Number(r.contract_multiplier) === 1 ? unitAsset : `สัญญา (${r.contract_multiplier} ${unitAsset})`;
  return {
    id: String(r.instrument_id), venue: r.venue, symbol: r.symbol, market: r.market_type,
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
    vwapMethod: res.method,
  };
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

  // Not served by the API yet: reject with a reason the screens can show instead of guessing.
  quote(_id: string): Promise<Quote> {
    return Promise.reject(new SourceError("not_supported", "quote"));
  }
  observations(_id: string): Promise<Observation[]> {
    return Promise.reject(new SourceError("not_supported", "observations"));
  }
  details(_id: string, _tf: Timeframe, _lookback: Lookback): Promise<MarketDetails> {
    return Promise.reject(new SourceError("not_supported", "details"));
  }
  alertHistory(): Promise<AlertEvent[]> {
    return Promise.reject(new SourceError("not_supported", "alerts"));
  }
}
