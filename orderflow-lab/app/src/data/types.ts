// Shapes shared by every data adapter (demo fixtures now, the server API later).
// Prices are numbers for display only; money and fills in the journal use decimal strings.

export type MarketType = "perpetual" | "spot";

export interface Instrument {
  id: string;
  venue: string;
  symbol: string;
  market: MarketType;
  base: string;
  quote: string;
  /** what one unit of quantity is, e.g. "BTC" or "oz" */
  qtyUnit: string;
  /** "linear" pays PnL in quote currency; "inverse" in base currency */
  contract: "linear" | "inverse" | "spot";
  priceDecimals: number;
  /** things a beginner must not confuse this with, shown next to the name */
  caution?: string;
}

/** How trustworthy a value is right now. Never shown as one global green dot. */
export type DataKind = "demo" | "live" | "delayed" | "stale" | "approximate" | "unavailable";

export interface Freshness {
  kind: DataKind;
  /** exchange event time of the newest value, unix ms; null when there is none */
  asOf: number | null;
  delayMin?: number;
}

export type PriceType = "last_trade" | "mark" | "index";

export interface Quote {
  instrumentId: string;
  price: number | null;
  priceType: PriceType;
  changePct: number | null;
  changeWindow: "24h";
  freshness: Freshness;
  spark: number[];
}

export type Timeframe = "1m" | "5m" | "15m" | "1h";
export type Lookback = "6h" | "24h" | "3d";

export interface Bar {
  t: number; // open time, unix seconds
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
  /** taker-buy volume; null when the source has no aggressor split (CVD would be an estimate) */
  buyV: number | null;
}

export interface Series {
  bars: Bar[];
  freshness: Freshness;
  /** where CVD starts accumulating; never moved silently when the timeframe changes */
  cvdAnchor: { label: string; startT: number };
  vwapMethod: string;
}

export type ObservationKind = "cvd" | "oi" | "wall" | "bigtrade" | "funding";
export type EvidenceTab = "chart" | "walls" | "oi" | "quality";

export interface Observation {
  id: string;
  instrumentId: string;
  kind: ObservationKind;
  title: string;
  /** what was measured, with units and window — a fact, not a forecast */
  measured: string;
  explain: string;
  at: number;
  evidence: EvidenceTab;
}

export type StreamName = "trades" | "book" | "kline" | "oi" | "mark" | "liquidations";
export type StreamStatus = "ok" | "stale" | "book_resync" | "disconnected" | "incomplete" | "unavailable";

export interface StreamQuality {
  stream: StreamName;
  status: StreamStatus;
  lastAt: number | null;
  note?: string;
}

export interface WallRow {
  side: "bid" | "ask";
  price: number;
  size: number;
  ageSec: number;
  status: "new" | "holding" | "adding" | "pulling";
}

export interface WallEvent {
  side: "bid" | "ask";
  price: number;
  size: number;
  /** the tracker cannot know whether a wall was filled, only what price did when it vanished */
  outcome: "price_reached" | "removed" | "unknown";
  at: number;
}

export interface BigTrade {
  at: number;
  side: "buy" | "sell";
  price: number;
  size: number;
}

export interface OiInfo {
  value: number | null;
  unit: string;
  change5mPct: number | null;
  change1hPct: number | null;
  funding: { ratePct: number; kind: "estimated" | "settled"; nextAt: number | null } | null;
  /** t = unix seconds */
  series: { t: number; v: number }[];
}

export interface MarketDetails {
  walls: WallRow[];
  wallEvents: WallEvent[];
  wallMethod: string;
  bigTrades: BigTrade[];
  bigMethod: string;
  oi: OiInfo | null;
  quality: StreamQuality[];
  profile: { binSize: number; poc: number | null; vaLow: number | null; vaHigh: number | null; method: string } | null;
  /** mark/index shown with their type so they are never swapped with last trade */
  prices: { type: PriceType; value: number | null }[];
}

export type AlertMetric = "oi_change_pct" | "cvd_change" | "price_cross" | "wall_size" | "funding_rate";

export interface AlertEvent {
  id: string;
  ruleId: string | null;
  instrumentId: string;
  metric: AlertMetric;
  title: string;
  measured: number;
  threshold: number;
  unit: string;
  window: string;
  at: number;
  /** backfilled history is never delivered as a live alert */
  live: boolean;
  dedupeKey: string;
  ack: boolean;
  /** which data source the page was using when it measured this (older events have none) */
  source?: "demo" | "api" | "connector";
}
