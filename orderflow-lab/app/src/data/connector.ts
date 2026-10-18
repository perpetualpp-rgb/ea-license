// Owner-preview adapter, bundled ONLY into the claude.ai artifact build (vite --mode artifact).
// Reads real Binance market data through the viewer's own Binance connector, the same way
// desk/template.html does: window.claude.use("mcp") → callTool("Binance", "tool_execute",
// { toolName, arguments }, { cache: false }). Read-only: every call is checked against BIN_ALLOW
// (public market data only) before it is sent, so no order, account or transfer tool is reachable.
// The data is for the owner's private viewing inside claude.ai; it is never re-published or stored
// on a server. Formulas for walls, big trades and the profile come from web/engine.js (domain/engine.ts).
import { DEMO_INSTRUMENTS } from "./demo";
import { SourceError, type ConnectorIssue, type MarketSource } from "./source";
import type {
  Bar, BigTrade, Instrument, Lookback, MarketDetails, Observation, OiInfo, Quote, Series, StreamQuality, Timeframe, WallEvent, WallRow,
} from "./types";
import { sessionStart } from "../domain/indicators";
import { WallTracker, bigTrades, bucketBook, poc, profileFromBars, sortBook, threshold, valueArea, type Book, type Level } from "../domain/engine";
import { fmtHM, fmtNum, fmtSigned } from "../lib/format";

export const BIN = "Binance";

/** the only Binance tools this page may call — the desk's set, all public market data */
export const BIN_ALLOW: ReadonlySet<string> = new Set([
  "futures_usds.klineCandlestickData", "futures_usds.orderBook", "futures_usds.compressedAggregateTradesList",
  "futures_usds.symbolPriceTicker", "spot.klines", "spot.depth", "spot.aggTrades",
  "futures_usds.openInterestStatistics", "futures_usds.markPrice", "futures_usds.topTraderLongShortRatioPositions", "futures_usds.takerBuySellVolume",
]);

const TOOLS = {
  fut: { k: "futures_usds.klineCandlestickData", d: "futures_usds.orderBook", t: "futures_usds.compressedAggregateTradesList", kmax: 1500 },
  spot: { k: "spot.klines", d: "spot.depth", t: "spot.aggTrades", kmax: 1000 },
} as const;

/** bucket = order-book bucket for walls (desk values), bin = profile level size (demo values) */
const CFG: Record<string, { mkt: "fut" | "spot"; symbol: string; bucket: number; bin: number }> = {
  "binance-um-btcusdt": { mkt: "fut", symbol: "BTCUSDT", bucket: 10, bin: 25 },
  "binance-um-xauusdt": { mkt: "fut", symbol: "XAUUSDT", bucket: 0.5, bin: 0.5 },
  "binance-spot-btcusdt": { mkt: "spot", symbol: "BTCUSDT", bucket: 10, bin: 25 },
};

type Interval = Timeframe | "30m";
const SEC: Record<Interval, number> = { "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600 };
const LOOKBACK_SEC: Record<Lookback, number> = { "6h": 6 * 3600, "24h": 86400, "3d": 3 * 86400 };
const MAX_SPAN_SEC = 3 * 86400;
const MAX_PAGES = 6; // 3 days of 1m spot klines = 5 pages of 1000

// how long one answer is reused by every screen that asks within that window (ms)
const TTL = { bars: 5_000, spark: 60_000, trades: 5_000, book: 5_000, oi: 60_000, mark: 20_000 };
/** newest exchange event older than this → "stale" (quotes, trades, book, mark) */
export const LIVE_MS = 120_000;
const OI_STALE_MS = 15 * 60_000; // 5-minute statistics arrive with a lag
const KEEP_TRADES_MS = 4 * 3600_000;
/** walls not looked at for longer than this are closed as "unknown" and counted again from zero */
const BOOK_GAP_MS = 90_000;
export const TRACKER = { minMult: 4, maxLevels: 200, mergeGap: 0, persistMs: 60000, maxDistPct: 1.0, missLimit: 2, minSeen: 2 };

// ---- the claude.ai mcp capability (only what this file uses) ---------------------------------
export interface McpResult { payload?: unknown; structuredContent?: unknown; content?: unknown }
export interface McpLike {
  callTool(server: string, tool: string, input?: unknown, options?: { cache?: false }): Promise<McpResult>;
}

// ---- errors: claude.ai mcp codes → SourceError codes + the desk's Thai wording ---------------
/** codes after which the connector cannot be used in this view without the owner doing something */
export const DENY = new Set(["needs_reauth", "server_not_connected", "blocked_by_policy", "approval_required", "not_in_manifest", "selection_required",
  "consent_required", "not_granted", "capability_disabled", "capability_removed", "no_mcp", "user_changed"]);

export class ConnectorError extends SourceError {
  constructor(code: SourceError["code"], message: string, userText: string, public connectorCode: string) {
    super(code, message, userText);
  }
}

const NAME = "Binance";
export const NO_HOST_TEXT = `เปิดหน้านี้ใน claude.ai เพื่อให้ดึงข้อมูลผ่าน connector ${NAME} ของคุณได้`;

export function mapMcpError(e: unknown, tool = ""): ConnectorError {
  if (e instanceof ConnectorError) return e;
  const o = (e && typeof e === "object" ? e : {}) as { code?: unknown; message?: unknown };
  const code = typeof o.code === "string" ? o.code : "unknown";
  const msg = typeof o.message === "string" ? o.message : String(e ?? "");
  const E = (c: SourceError["code"], text: string) => new ConnectorError(c, `${code}: ${msg}`.slice(0, 300), text, code);
  switch (code) {
    case "server_not_connected": return E("not_configured", `ยังไม่ได้เชื่อม connector ${NAME} เพิ่มได้ที่ claude.ai → Settings → Connectors แล้วเปิดหน้านี้ใหม่`);
    case "needs_reauth": return E("unauthorized", `การเชื่อมต่อ ${NAME} หมดอายุ เชื่อมต่อใหม่ที่ claude.ai → Settings → Connectors`);
    case "not_in_manifest": return E("unauthorized", `หน้านี้ยังไม่ได้รับอนุญาตให้ใช้ ${NAME} เปิดเมนู Permissions ของหน้านี้แล้วเปิดสิทธิ์`);
    case "selection_required": return E("not_configured", `มี connector ชื่อ ${NAME} มากกว่าหนึ่งตัว เลือกตัวที่จะใช้ในหน้าต่างที่ claude.ai แสดง`);
    case "approval_required": case "consent_required": return E("unauthorized", `ต้องอนุญาตให้หน้านี้ใช้ ${NAME} ก่อน กดปุ่ม "อนุญาตและดึงข้อมูล"`);
    case "blocked_by_policy": return E("unauthorized", `องค์กรของคุณปิดการใช้ ${NAME}`);
    case "tool_error": {
      // Binance's own error codes ride in the message: -1003 too many requests, -1121 invalid symbol
      const c = /-1003\b|\b429\b|\b418\b/.test(msg) ? "rate_limited" : /-1121\b/.test(msg) ? "not_supported" : "server";
      return E(c, `${NAME} ตอบว่าดึงข้อมูลไม่สำเร็จ: ${msg.slice(0, 160)}`);
    }
    case "server_unavailable": return E("network", `${NAME} ไม่ตอบชั่วคราว แสดงข้อมูลล่าสุดที่มีไว้ก่อน`);
    case "rate_limited": return E("rate_limited", `${NAME} ไม่ตอบชั่วคราว แสดงข้อมูลล่าสุดที่มีไว้ก่อน`);
    case "not_granted": case "capability_disabled": case "capability_removed": return E("not_configured", "มุมมองนี้เรียก connector ไม่ได้ เปิดหน้านี้ใน claude.ai");
    case "no_mcp": return E("not_configured", NO_HOST_TEXT);
    case "user_changed": return E("unauthorized", "บัญชีที่เปิดหน้านี้เปลี่ยนไป เปิดหน้านี้ใหม่");
    case "blocked_tool": return E("not_supported", `หน้านี้อ่านข้อมูลตลาดเท่านั้น ไม่เรียก ${tool || "เครื่องมือนี้"}`);
    case "bad_shape": return E("server", `${NAME} ตอบข้อมูลในรูปแบบที่หน้านี้ไม่รู้จัก (${tool})`);
    case "upstream_error": case "cancelled": return E("network", `ดึงข้อมูลจาก ${NAME} ไม่สำเร็จ (${code})`);
    default: return E("server", `ดึงข้อมูลจาก ${NAME} ไม่สำเร็จ (${code || "ไม่ทราบสาเหตุ"})`);
  }
}

export function toIssue(e: ConnectorError): ConnectorIssue {
  const c = e.connectorCode;
  return { code: c, text: e.userText ?? e.message, approve: c === "approval_required" || c === "consent_required", retry: c !== "user_changed" };
}

// ---- payloads: unwrap like the desk, then parse Binance's REST shapes ------------------------
const asObj = (p: unknown): unknown => {
  if (typeof p === "string") { try { return JSON.parse(p); } catch { return p; } }
  return p;
};
type Rec = Record<string, unknown>;
const isRec = (p: unknown): p is Rec => !!p && typeof p === "object" && !Array.isArray(p);

/** the connector's answer → Binance's JSON; Binance error bodies and plain-text failures become tool_error */
export function unwrapPayload(res: McpResult | undefined): unknown {
  let raw: unknown = res?.payload;
  if (raw === undefined) raw = res?.structuredContent;
  if (raw === undefined && Array.isArray(res?.content)) raw = (res!.content as { type?: string; text?: string }[]).find((b) => b?.type === "text")?.text;
  let p = asObj(raw);
  if (isRec(p) && p.result !== undefined && Object.keys(p).length <= 2) p = asObj(p.result);
  if (typeof p === "string") throw { code: "tool_error", message: p };
  if (isRec(p) && p.code !== undefined && p.msg) throw { code: "tool_error", message: `${p.code} ${p.msg}` };
  return p;
}

const fin = (...xs: number[]) => xs.every(Number.isFinite);

/** klines: [openTime, open, high, low, close, volume, closeTime, quoteVolume, trades, takerBuyBase, takerBuyQuote, ignore] */
export function parseKlines(rows: unknown): Bar[] {
  if (!Array.isArray(rows)) throw { code: "bad_shape", message: "klines" };
  const out: Bar[] = [];
  for (const r of rows) {
    if (!Array.isArray(r) || r.length < 6) continue;
    const b: Bar = { t: Math.floor(+r[0] / 1000), o: +r[1], h: +r[2], l: +r[3], c: +r[4], v: +r[5], buyV: r[9] == null || !Number.isFinite(+r[9]) ? null : +r[9] };
    if (fin(b.t, b.o, b.h, b.l, b.c, b.v) && b.v >= 0) out.push(b);
  }
  return out.sort((a, z) => a.t - z.t);
}

export interface AggTrade { id: number; ts: number; price: number; size: number; side: "buy" | "sell" }
/** aggTrades: {a: id, p: price, q: qty, f, l, T: time, m: buyer is maker → the taker sold} */
export function parseAggTrades(rows: unknown): AggTrade[] {
  if (!Array.isArray(rows)) throw { code: "bad_shape", message: "aggTrades" };
  const out: AggTrade[] = [];
  for (const r of rows) {
    if (!isRec(r)) continue;
    const t: AggTrade = { id: +(r.a as number), ts: +(r.T as number), price: +(r.p as string), size: +(r.q as string), side: r.m ? "sell" : "buy" };
    if (fin(t.id, t.ts, t.price, t.size)) out.push(t);
  }
  return out.sort((a, z) => a.id - z.id);
}

export interface DepthBook extends Book { exchangeTime: boolean }
/** depth: {lastUpdateId, E?, T?, bids: [[price, qty]], asks}; spot has no E/T, so its time is when the page received it */
export function parseDepth(p: unknown, receivedAt: number, symbol: string): DepthBook {
  if (!isRec(p) || !Array.isArray(p.bids) || !Array.isArray(p.asks)) throw { code: "bad_shape", message: "depth" };
  const lv = (a: unknown[]): Level[] => a.filter(Array.isArray).map((l) => [+l[0], +l[1]] as Level).filter((l) => fin(l[0], l[1]));
  const et = +((p.T ?? p.E) as number);
  return { ts: Number.isFinite(et) && et > 0 ? et : receivedAt, exchangeTime: Number.isFinite(et) && et > 0, symbol, ...sortBook(lv(p.bids), lv(p.asks)) };
}

export interface MarkInfo { mark: number | null; index: number | null; ratePct: number | null; nextAt: number | null; time: number | null }
/** markPrice (premiumIndex): {symbol, markPrice, indexPrice, lastFundingRate, nextFundingTime, time}; an array when no symbol was sent */
export function parseMark(p: unknown, symbol: string): MarkInfo {
  const r = Array.isArray(p) ? p.find((x) => isRec(x) && x.symbol === symbol) : p;
  if (!isRec(r)) throw { code: "bad_shape", message: "markPrice" };
  const n = (v: unknown) => (v == null || v === "" || !Number.isFinite(+(v as number)) ? null : +(v as number));
  const rate = n(r.lastFundingRate);
  return { mark: n(r.markPrice), index: n(r.indexPrice), ratePct: rate == null ? null : rate * 100, nextAt: n(r.nextFundingTime) || null, time: n(r.time) };
}

/** openInterestStatistics (openInterestHist): [{symbol, sumOpenInterest (base asset), sumOpenInterestValue, timestamp}] */
export function parseOi(rows: unknown): { t: number; v: number }[] {
  if (!Array.isArray(rows)) throw { code: "bad_shape", message: "openInterestStatistics" };
  return rows.filter(isRec).map((r) => ({ t: Math.floor(+(r.timestamp as number) / 1000), v: +(r.sumOpenInterest as string) }))
    .filter((r) => fin(r.t, r.v)).sort((a, z) => a.t - z.t);
}

export function oiInfo(series: { t: number; v: number }[], unit: string): Pick<OiInfo, "value" | "unit" | "change5mPct" | "change1hPct" | "series"> {
  const last = series.at(-1);
  const pct = (backSec: number) => {
    if (!last) return null;
    const prev = series.find((p) => p.t === last.t - backSec); // only an exact 5m / 1h step, never a nearby window
    return prev && prev.v > 0 ? (last.v / prev.v - 1) * 100 : null;
  };
  return { value: last?.v ?? null, unit, change5mPct: pct(300), change1hPct: pct(3600), series };
}

/** "live" only when the newest exchange event is within `windowMs` of now; never "demo" */
export function freshness(asOf: number | null, now: number, windowMs = LIVE_MS): Quote["freshness"] {
  if (asOf == null) return { kind: "unavailable", asOf: null };
  return { kind: now - asOf <= windowMs ? "live" : "stale", asOf };
}
/** a series is live within 2 bars, and never more than 120 s */
export const seriesWindowMs = (tf: Timeframe) => Math.min(2 * SEC[tf] * 1000, LIVE_MS);

// ---- per-stream memory ---------------------------------------------------------------------
interface Attempt { at: number; err: ConnectorError | null }
interface BarStore extends Attempt { bars: Bar[]; from: number; inflight: Promise<void> | null }
interface TradeStore extends Attempt { trades: AggTrade[]; lastId: number | null; gaps: number; lastPollGap: boolean; inflight: Promise<void> | null }
interface BookStore extends Attempt { tracker: WallTracker; book: DepthBook | null; okAt: number; fails: number; reset: string | null; inflight: Promise<void> | null }
interface Cached<T> extends Attempt { value: T | null; inflight: Promise<void> | null }

export interface ConnectorOptions {
  now?: () => number;
  /** told once per failure that blocks the connector (not connected, denied, approval needed …) */
  onIssue?: (issue: ConnectorIssue) => void;
  /** wait before the single retry of a retryable error; tests pass 0 */
  retryDelayMs?: number;
}

export class ConnectorSource implements MarketSource {
  readonly mode = "connector" as const;
  private now: () => number;
  private mcpP: Promise<McpLike | null> | null = null;
  private barStores = new Map<string, BarStore>();
  private tradeStores = new Map<string, TradeStore>();
  private bookStores = new Map<string, BookStore>();
  private caches = new Map<string, Cached<unknown>>();

  constructor(private getMcp: () => Promise<McpLike | null>, private opts: ConnectorOptions = {}) {
    this.now = opts.now ?? Date.now;
  }

  // ---- the one door to Binance ---------------------------------------------------------------
  private async mcp(): Promise<McpLike> {
    if (!this.mcpP) this.mcpP = this.getMcp().catch(() => null);
    const m = await this.mcpP;
    if (!m) { this.mcpP = null; throw mapMcpError({ code: "no_mcp", message: "use(mcp) resolved null" }); }
    return m;
  }

  /** allowlist first, then one call; a retryable failure is retried once after a short wait */
  async bin(tool: string, args: Record<string, unknown>): Promise<unknown> {
    if (!BIN_ALLOW.has(tool)) throw mapMcpError({ code: "blocked_tool", message: "blocked: " + tool }, tool);
    try {
      const mcp = await this.mcp();
      const call = () => mcp.callTool(BIN, "tool_execute", { toolName: tool, arguments: args }, { cache: false });
      let res: McpResult;
      try {
        res = await call();
      } catch (e) {
        const r = e as { retryable?: boolean; retryAfterMs?: number };
        if (!r || r.retryable !== true) throw e;
        const wait = this.opts.retryDelayMs ?? Math.min(r.retryAfterMs ?? 0, 3000) + 300 + Math.random() * 500;
        if (wait > 0) await new Promise((ok) => setTimeout(ok, wait));
        res = await call();
      }
      return unwrapPayload(res);
    } catch (e) {
      const err = mapMcpError(e, tool);
      if (DENY.has(err.connectorCode)) this.opts.onIssue?.(toIssue(err));
      throw err;
    }
  }

  /** one call from a click (it may ask the owner to allow the connector); null when it worked */
  async probe(): Promise<ConnectorIssue | null> {
    try {
      await this.bin("futures_usds.symbolPriceTicker", { symbol: "BTCUSDT" });
      return null;
    } catch (e) {
      return toIssue(mapMcpError(e));
    }
  }

  private cfg(id: string) {
    const c = CFG[id];
    if (!c) throw new SourceError("not_supported", id);
    return { ...c, T: TOOLS[c.mkt], ins: DEMO_INSTRUMENTS.find((i) => i.id === id) as Instrument };
  }

  /** one shared answer per key for `ttl` ms (and while `valid` holds), concurrent askers share one call */
  private async cached<T>(key: string, ttl: number, fetch: () => Promise<T>, valid: (v: T) => boolean = () => true): Promise<T> {
    let c = this.caches.get(key) as Cached<T> | undefined;
    if (!c) { c = { at: 0, err: null, value: null, inflight: null }; this.caches.set(key, c as Cached<unknown>); }
    const st = c;
    if (st.inflight) await st.inflight;
    if (st.value != null && !st.err && this.now() - st.at < ttl && valid(st.value)) return st.value;
    st.inflight = (async () => {
      try { st.value = await fetch(); st.err = null; }
      catch (e) { st.err = mapMcpError(e); }
      finally { st.at = this.now(); st.inflight = null; }
    })();
    await st.inflight;
    if (st.err) throw st.err;
    return st.value as T;
  }

  // ---- klines: one store per instrument × interval, extended forward page by page ----------------
  /** bars already held for this window (the last good answer), or null when they do not cover it */
  private heldBars(id: string, iv: Interval, fromS: number): Bar[] | null {
    const st = this.barStores.get(`${id}|${iv}`);
    const start0 = Math.floor(fromS / SEC[iv]) * SEC[iv];
    return st && st.bars.length && st.from <= start0 ? st.bars.filter((b) => b.t >= start0) : null;
  }
  /** a failed refresh keeps showing the last good bars (their time decides live/stale); with nothing held it fails */
  private async barsOrHeld(id: string, iv: Interval, fromS: number): Promise<Bar[]> {
    try { return await this.ensureBars(id, iv, fromS); }
    catch (e) { const held = DENY.has(mapMcpError(e).connectorCode) ? null : this.heldBars(id, iv, fromS); if (held) return held; throw e; }
  }

  private async ensureBars(id: string, iv: Interval, fromS: number): Promise<Bar[]> {
    const { symbol, T } = this.cfg(id);
    const key = `${id}|${iv}`;
    let st = this.barStores.get(key);
    if (!st) { st = { bars: [], from: Infinity, at: 0, err: null, inflight: null }; this.barStores.set(key, st); }
    const s = st;
    const sec = SEC[iv];
    const start0 = Math.floor(fromS / sec) * sec;
    const pick = () => s.bars.filter((b) => b.t >= start0);
    if (s.inflight) await s.inflight;
    if (s.bars.length && s.from <= start0 && !s.err && this.now() - s.at < (iv === "30m" ? TTL.spark : TTL.bars)) return pick();
    s.inflight = (async () => {
      const now = this.now();
      const nowS = Math.floor(now / 1000);
      const scratch = !s.bars.length || s.from > start0;
      let start = scratch ? start0 : s.bars[s.bars.length - 1].t; // re-read the forming bar
      const fresh: Bar[] = [];
      try {
        for (let page = 0; page < MAX_PAGES; page++) {
          const limit = Math.min(T.kmax, Math.ceil((nowS - start) / sec) + 2);
          const rows = parseKlines(await this.bin(T.k, { symbol, interval: iv, startTime: start * 1000, limit }));
          fresh.push(...rows);
          if (rows.length < limit) break;
          start = rows[rows.length - 1].t + sec;
          if (start > nowS) break;
        }
        let bars = scratch ? fresh : fresh.length ? [...s.bars.filter((b) => b.t < fresh[0].t), ...fresh] : s.bars;
        const keepFrom = Math.floor((nowS - MAX_SPAN_SEC) / sec) * sec - sec;
        bars = bars.filter((b, i) => b.t >= keepFrom && (i === 0 || b.t !== bars[i - 1].t));
        s.bars = bars;
        s.from = Math.max(scratch ? start0 : s.from, keepFrom);
        s.err = null;
      } catch (e) {
        s.err = mapMcpError(e, T.k);
      } finally {
        s.at = this.now();
        s.inflight = null;
      }
    })();
    await s.inflight;
    if (s.err) throw s.err;
    return pick();
  }

  // ---- aggTrades: the newest 1000, then forward by id; if the page falls behind it jumps to the newest and counts a gap
  private async ensureTrades(id: string): Promise<TradeStore> {
    const { symbol, T } = this.cfg(id);
    let st = this.tradeStores.get(id);
    if (!st) { st = { trades: [], lastId: null, gaps: 0, lastPollGap: false, at: 0, err: null, inflight: null }; this.tradeStores.set(id, st); }
    const s = st;
    if (s.inflight) await s.inflight;
    if (s.at && !s.err && this.now() - s.at < TTL.trades) return s;
    s.inflight = (async () => {
      try {
        const add = (rows: AggTrade[]) => {
          for (const r of rows) if (s.lastId == null || r.id > s.lastId) { s.trades.push(r); s.lastId = r.id; }
        };
        let gap = false;
        if (s.lastId == null) add(parseAggTrades(await this.bin(T.t, { symbol, limit: 1000 })));
        else {
          let behind = false;
          for (let page = 0; page < 2; page++) {
            const rows = parseAggTrades(await this.bin(T.t, { symbol, fromId: s.lastId + 1, limit: 1000 }));
            add(rows);
            behind = rows.length >= 1000;
            if (!behind) break;
          }
          if (behind) {
            const rows = parseAggTrades(await this.bin(T.t, { symbol, limit: 1000 }));
            if (rows.length && s.lastId != null && rows[0].id > s.lastId + 1) { gap = true; s.gaps += 1; }
            add(rows);
          }
        }
        s.lastPollGap = gap;
        const newest = s.trades.at(-1)?.ts;
        if (newest != null && s.trades[0].ts < newest - KEEP_TRADES_MS) s.trades = s.trades.filter((t) => t.ts >= newest - KEEP_TRADES_MS);
        s.err = null;
      } catch (e) {
        s.err = mapMcpError(e, T.t);
      } finally {
        s.at = this.now();
        s.inflight = null;
      }
    })();
    await s.inflight;
    if (s.err) throw s.err;
    return s;
  }

  // ---- order book: REST snapshot → buckets → the engine's wall tracker (kept across polls) -----
  private async updateBook(id: string): Promise<BookStore> {
    const { symbol, T, bucket } = this.cfg(id);
    let st = this.bookStores.get(id);
    if (!st) { st = { tracker: new WallTracker(bucket, TRACKER), book: null, okAt: 0, fails: 0, reset: null, at: 0, err: null, inflight: null }; this.bookStores.set(id, st); }
    const s = st;
    if (s.inflight) await s.inflight;
    if (s.at && !s.err && this.now() - s.at < TTL.book) return s;
    s.inflight = (async () => {
      const now = this.now();
      try {
        // not watched for a while: walls could have come and gone unseen, so close them as unknown
        if (s.okAt && now - s.okAt > BOOK_GAP_MS && s.tracker.walls.size) {
          s.tracker.bookLost(s.book?.ts ?? now);
          s.reset = `ไม่ได้ดู book นานกว่า ${BOOK_GAP_MS / 1000} วินาที กำแพงเดิมถูกปิดเป็น "ไม่ทราบ" และเริ่มนับใหม่`;
        }
        let p: unknown;
        try {
          p = await this.bin(T.d, { symbol, limit: 1000 });
          s.fails = 0;
        } catch (e) {
          if (++s.fails === 2) { s.tracker.bookLost(s.book?.ts ?? now); s.reset = "ดึง book ไม่ได้ 2 ครั้งติด กำแพงที่ติดตามอยู่ถูกปิดเป็น \"ไม่ทราบ\""; }
          throw e;
        }
        const book = parseDepth(p, now, symbol);
        s.tracker.update(bucketBook(book, bucket));
        s.book = book;
        s.okAt = now;
        s.err = null;
      } catch (e) {
        s.err = mapMcpError(e, T.d);
      } finally {
        s.at = this.now();
        s.inflight = null;
      }
    })();
    await s.inflight;
    if (s.err) throw s.err;
    return s;
  }

  private oi(id: string) {
    const { symbol } = this.cfg(id);
    return this.cached(`${id}|oi`, TTL.oi, async () => parseOi(await this.bin("futures_usds.openInterestStatistics", { symbol, period: "5m", limit: 289 })));
  }
  private mark(id: string) {
    const { symbol } = this.cfg(id);
    return this.cached(`${id}|mark`, TTL.mark, async () => parseMark(await this.bin("futures_usds.markPrice", { symbol }), symbol));
  }
  /** open of the 1m kline that starts at the first minute inside the last 24 h (Binance's rolling-24h sense) */
  private refPrice(id: string, now: number) {
    const { symbol, T } = this.cfg(id);
    const startMs = Math.ceil((now - 86_400_000) / 60_000) * 60_000;
    return this.cached(`${id}|ref`, 10 * 60_000, async () => {
      const b = parseKlines(await this.bin(T.k, { symbol, interval: "1m", startTime: startMs, limit: 1 }))[0];
      return { startMs, o: b && b.t * 1000 === startMs ? b.o : null }; // a missing minute stays null, never a nearby one
    }, (v) => v.startMs === startMs);
  }

  // ---- MarketSource ------------------------------------------------------------------------------
  async instruments(): Promise<Instrument[]> {
    return DEMO_INSTRUMENTS.filter((i) => CFG[i.id]);
  }

  async quote(id: string): Promise<Quote> {
    this.cfg(id);
    const now = this.now();
    const nowS = Math.floor(now / 1000);
    const [tr, spark, ref] = await Promise.allSettled([this.ensureTrades(id), this.barsOrHeld(id, "30m", nowS - 86400), this.refPrice(id, now)]);
    // a failed refresh falls back to the newest trade already held; its exchange time makes it stale soon enough
    const held = tr.status === "rejected" && !DENY.has(mapMcpError(tr.reason).connectorCode) ? this.tradeStores.get(id)?.trades : undefined;
    const lastTrade = tr.status === "fulfilled" ? tr.value.trades.at(-1) : held?.at(-1);
    const sparkBars = spark.status === "fulfilled" ? spark.value : [];
    const lastBar = sparkBars.at(-1);
    if (!lastTrade && !lastBar) throw (tr.status === "rejected" ? tr.reason : spark.status === "rejected" ? spark.reason : new SourceError("server", "no data", `${NAME} ยังไม่มีรายการซื้อขายสำหรับสัญญานี้`));
    // newest aggTrade gives price and exchange time; a kline close only proves a trade at/after its open
    const price = lastTrade ? lastTrade.price : lastBar!.c;
    const asOf = lastTrade ? lastTrade.ts : lastBar!.t * 1000;
    const refO = ref.status === "fulfilled" ? ref.value.o : null;
    return {
      instrumentId: id, price, priceType: "last_trade", changeWindow: "24h",
      changePct: refO ? (price / refO - 1) * 100 : null,
      freshness: freshness(asOf, now),
      spark: sparkBars.filter((b) => b.t >= nowS - 86400 - 1800).map((b) => b.c),
    };
  }

  async series(id: string, tf: Timeframe, lookback: Lookback): Promise<Series> {
    this.cfg(id);
    const now = this.now();
    const nowS = Math.floor(now / 1000);
    const fromS = nowS - LOOKBACK_SEC[lookback];
    const [bars, tr] = await Promise.all([this.barsOrHeld(id, tf, fromS), this.ensureTrades(id).catch(() => null)]);
    const last = bars.at(-1);
    const newestTrade = tr?.trades.at(-1)?.ts ?? null;
    const asOf = last ? Math.max(last.t * 1000, newestTrade ?? 0) : null;
    const anchor = sessionStart(nowS);
    const first = bars[0]?.t ?? fromS;
    const fromSession = anchor >= first;
    return {
      bars,
      freshness: freshness(asOf, now, seriesWindowMs(tf)),
      cvdAnchor: fromSession ? { label: "เริ่มนับ 07:00 UTC+7", startT: anchor } : { label: `เริ่มนับจากแท่งแรกของช่วงที่เลือก (${fmtHM(first * 1000)} UTC+7)`, startT: first },
      vwapMethod: `typical price × volume ของแท่ง ${tf} จาก Binance ตั้งแต่จุดเริ่มนับ CVD (ประมาณจากแท่ง ไม่ใช่ VWAP จากทุกรายการซื้อขาย)`,
    };
  }

  async details(id: string, tf: Timeframe, lookback: Lookback): Promise<MarketDetails> {
    const { ins, mkt, bin, bucket } = this.cfg(id);
    const perp = mkt === "fut";
    const now = this.now();
    const fromS = Math.floor(now / 1000) - LOOKBACK_SEC[lookback];
    const [barsR, tradesR, bookR, oiR, markR] = await Promise.allSettled([
      this.ensureBars(id, tf, fromS), this.ensureTrades(id), this.updateBook(id),
      perp ? this.oi(id) : Promise.resolve(null), perp ? this.mark(id) : Promise.resolve(null),
    ]);
    const tried = perp ? [barsR, tradesR, bookR, oiR, markR] : [barsR, tradesR, bookR];
    if (tried.every((r) => r.status === "rejected")) throw (barsR as PromiseRejectedResult).reason;

    const bars = barsR.status === "fulfilled" ? barsR.value : this.heldBars(id, tf, fromS) ?? [];
    const ts = this.tradeStores.get(id);
    const bs = this.bookStores.get(id);
    const trades = tradesR.status === "fulfilled" ? tradesR.value.trades : ts?.trades ?? [];
    const markV = markR.status === "fulfilled" ? markR.value : null;
    const oiV = oiR.status === "fulfilled" ? oiR.value : null;

    // walls only from a book this call actually read
    const bookOk = bookR.status === "fulfilled";
    const walls: WallRow[] = bookOk && bs ? bs.tracker.active().slice(0, 8).map((w) => ({
      side: w.side, price: w.price, size: w.size, ageSec: Math.max(0, Math.round(w.age_ms / 1000)), status: w.status,
    })) : [];
    const wallEvents: WallEvent[] = bs ? bs.tracker.events(6).reverse().map((e) => ({ side: e.side, price: e.price, size: e.size, outcome: e.outcome, at: e.ended_ts })) : [];

    // big trades: the engine's percentile threshold over the trades this page holds
    const thr = threshold(trades, 0.99);
    const big: BigTrade[] = bigTrades(trades, thr).slice(-8).reverse().map((t) => ({ at: t.ts, side: t.side, price: t.price, size: t.size }));
    const unit = ins.qtyUnit;
    const bigMethod = trades.length
      ? `ไม้ ≥ เปอร์เซ็นไทล์ที่ 99 (≥ ${fmtNum(thr, thr >= 10 ? 1 : 3)} ${unit}) ของ ${trades.length.toLocaleString("en-US")} aggTrade ที่หน้านี้ดึงมาตั้งแต่ ${fmtHM(trades[0].ts)} UTC+7 (เก็บย้อนสูงสุด 4 ชม. นับไม้ล่าสุดในเกณฑ์ด้วย) · 1 aggTrade อาจรวมหลายคำสั่งที่ราคาเดียวกัน${ts?.gaps ? ` · ขาดช่วง ${ts.gaps} ครั้งเพราะดึงไม่ทัน ไม้ในช่วงนั้นไม่แสดง` : ""}`
      : "ยังไม่มีรายการซื้อขายจาก Binance";

    const prof = bars.length ? profileFromBars(bars, bin) : null;
    const va = prof ? valueArea(prof, 0.7) : null;
    const lastPx = trades.at(-1)?.price ?? bars.at(-1)?.c ?? null;

    let oi: OiInfo | null = null;
    if (perp) {
      const base = oiInfo(oiV ?? [], ins.base);
      oi = { ...base, funding: markV && markV.ratePct != null ? { ratePct: markV.ratePct, kind: "estimated", nextAt: markV.nextAt } : null };
    }

    return {
      walls, wallEvents,
      wallMethod: `book รวมเป็นช่วงละ ${bucket} ${ins.quote} · กำแพง = ขนาด ≥ 4 เท่าของค่ามัธยฐาน 200 ช่วงแรกแต่ละฝั่ง ภายใน ±1% ของราคากลาง · ต้องเห็นอย่างน้อย 2 รอบจึงนับเป็นเหตุการณ์เมื่อหายไป · อายุนับจากที่หน้านี้เห็นครั้งแรก (ไม่ใช่เวลาที่คำสั่งถูกวาง) · อ่าน snapshot ของ book ทุกครั้งที่หน้าขอข้อมูล ไม่ได้เห็นทุกการเปลี่ยนแปลง`,
      bigTrades: big, bigMethod,
      oi,
      quality: this.quality(id, tf, now),
      profile: prof ? { binSize: bin, poc: poc(prof), vaLow: va?.[0] ?? null, vaHigh: va?.[1] ?? null,
        method: `กระจาย volume ของแท่ง ${tf} จาก Binance ตามช่วง high–low (ประมาณ ไม่ใช่ volume ที่ราคาจริง) · value area 70%` } : null,
      prices: [
        { type: "last_trade", value: lastPx },
        { type: "mark", value: perp ? markV?.mark ?? null : null },
        { type: "index", value: perp ? markV?.index ?? null : null },
      ],
    };
  }

  /** per stream: what the last call returned and when (exchange time), never one global dot */
  private quality(id: string, tf: Timeframe, now: number): StreamQuality[] {
    const perp = this.cfg(id).mkt === "fut";
    const errNote = (e: ConnectorError) => `${e.userText ?? e.message} · แสดงค่าล่าสุดที่ดึงได้`;
    const status = (a: Attempt | undefined, lastAt: number | null, staleMs: number, extra?: string): StreamQuality["status"] =>
      !a || (!a.at && !a.err) ? "unavailable" : a.err ? "disconnected" : lastAt == null ? "unavailable" : now - lastAt > staleMs ? "stale" : extra ? "incomplete" : "ok";

    const tr = this.tradeStores.get(id);
    const trLast = tr?.trades.at(-1)?.ts ?? null;
    const trGap = tr?.lastPollGap ? "รอบล่าสุดดึงรายการซื้อขายไม่ทัน ข้ามไปช่วงใหม่ล่าสุด รายการช่วงที่ข้ามไม่ครบ" : undefined;
    const trades: StreamQuality = { stream: "trades", status: status(tr, trLast, LIVE_MS, trGap), lastAt: trLast,
      note: tr?.err ? errNote(tr.err) : trGap ?? (tr?.gaps ? `aggTrades ขาดช่วง ${tr.gaps} ครั้งตั้งแต่เปิดหน้า` : undefined) };

    const bk = this.bookStores.get(id);
    const bkLast = bk?.book?.ts ?? null;
    const bookNote = [bk?.err ? errNote(bk.err) : null, bk?.reset, bk?.book && !bk.book.exchangeTime ? "spot depth ไม่มีเวลาของตลาด ใช้เวลาที่หน้าได้รับ" : null].filter(Boolean).join(" · ");
    const book: StreamQuality = { stream: "book", status: status(bk, bkLast, LIVE_MS), lastAt: bkLast, note: bookNote || undefined };

    const ks = this.barStores.get(`${id}|${tf}`);
    const kLast = ks?.bars.at(-1) ? ks.bars[ks.bars.length - 1].t * 1000 : null;
    const kline: StreamQuality = { stream: "kline", status: status(ks, kLast, 2 * SEC[tf] * 1000 + 30_000), lastAt: kLast, note: ks?.err ? errNote(ks.err) : undefined };

    const spotNote = (what: string): StreamQuality["note"] => `ตลาด spot ไม่มี ${what}`;
    if (!perp) return [trades, book, kline,
      { stream: "oi", status: "unavailable", lastAt: null, note: spotNote("Open Interest") },
      { stream: "mark", status: "unavailable", lastAt: null, note: spotNote("mark price") },
      { stream: "liquidations", status: "unavailable", lastAt: null, note: spotNote("liquidation") }];

    const oc = this.caches.get(`${id}|oi`) as Cached<{ t: number; v: number }[]> | undefined;
    const oLast = oc?.value?.at(-1) ? oc.value[oc.value.length - 1].t * 1000 : null;
    const mc = this.caches.get(`${id}|mark`) as Cached<MarkInfo> | undefined;
    const mLast = mc?.value?.time ?? null;
    return [trades, book, kline,
      { stream: "oi", status: status(oc, oLast, OI_STALE_MS), lastAt: oLast, note: oc?.err ? errNote(oc.err) : "สถิติ OI ราย 5 นาทีของ Binance ไม่ใช่ค่าทันที" },
      { stream: "mark", status: status(mc, mLast, LIVE_MS), lastAt: mLast, note: mc?.err ? errNote(mc.err) : undefined },
      { stream: "liquidations", status: "unavailable", lastAt: null, note: "หน้านี้ไม่ดึง liquidation (ไม่อยู่ในรายการข้อมูลที่อนุญาต)" }];
  }

  async observations(id: string): Promise<Observation[]> {
    const { ins, mkt } = this.cfg(id);
    const perp = mkt === "fut";
    const now = this.now();
    const nowS = Math.floor(now / 1000);
    const [b1, oiR, bookR, trR] = await Promise.allSettled([
      this.ensureBars(id, "1m", nowS - 600), perp ? this.oi(id) : Promise.resolve(null), this.updateBook(id), perp ? Promise.resolve(null) : this.ensureTrades(id),
    ]);
    const tried = perp ? [b1, oiR, bookR] : [b1, bookR, trR];
    if (tried.every((r) => r.status === "rejected")) throw (b1 as PromiseRejectedResult).reason;
    const out: Observation[] = [];
    const oz = ins.qtyUnit === "oz";

    const last5 = b1.status === "fulfilled" ? b1.value.slice(-5) : [];
    if (last5.length === 5 && last5.every((b) => b.buyV != null)) {
      const d5 = last5.reduce((a, b) => a + (b.buyV! - (b.v - b.buyV!)), 0);
      out.push({ id: `${id}-cvd`, instrumentId: id, kind: "cvd", evidence: "chart", at: Math.min(now, (last5[4].t + 60) * 1000),
        title: d5 >= 0 ? "CVD เพิ่มขึ้น" : "CVD ลดลง",
        measured: `CVD ${d5 >= 0 ? "+" : "−"}${Math.abs(d5).toFixed(oz ? 0 : 1)} ${ins.qtyUnit} ใน 5 นาที (แท่ง 1 นาที 5 แท่งล่าสุด)`,
        explain: d5 >= 0 ? "ปริมาณที่ผู้ซื้อกดซื้อทันที มากกว่าที่ผู้ขายกดขายทันที" : "ปริมาณที่ผู้ขายกดขายทันที มากกว่าที่ผู้ซื้อกดซื้อทันที" });
    }

    if (oiR.status === "fulfilled" && oiR.value) {
      const o = oiInfo(oiR.value, ins.base);
      if (o.change5mPct != null) out.push({ id: `${id}-oi`, instrumentId: id, kind: "oi", evidence: "oi", at: o.series.at(-1)!.t * 1000,
        title: o.change5mPct >= 0 ? "OI เพิ่มขึ้น" : "OI ลดลง",
        measured: `OI ${fmtSigned(o.change5mPct, 2, "%")} ใน 5 นาที (หน่วย ${o.unit})`,
        explain: "สถานะคงค้างเปลี่ยน ทุกสัญญามีทั้งฝั่ง Long และ Short จึงไม่บอกว่าฝั่งไหนมากกว่า" });
    }

    if (!perp && trR.status === "fulfilled" && trR.value) {
      const trades = trR.value.trades, thr = threshold(trades, 0.99), b = bigTrades(trades, thr).at(-1);
      if (b) out.push({ id: `${id}-big`, instrumentId: id, kind: "bigtrade", evidence: "walls", at: b.ts, title: "ไม้ใหญ่ล่าสุด",
        measured: `${b.side === "buy" ? "ซื้อ" : "ขาย"} ${fmtNum(b.size, 2)} ${ins.qtyUnit} ที่ ${b.price.toLocaleString("en-US")} ${ins.quote} · เกณฑ์ ≥ เปอร์เซ็นไทล์ 99 ของ ${trades.length.toLocaleString("en-US")} ไม้ที่หน้านี้ดึงมา`,
        explain: "ไม้ที่ใหญ่ผิดปกติเทียบกับรายการซื้อขายช่วงที่ผ่านมา 1 aggTrade อาจรวมหลายคำสั่ง ไม่บอกว่าเป็นใครหรือจะไปทางไหน" });
    }

    if (bookR.status === "fulfilled") {
      const act = bookR.value.tracker.active();
      const w = act.find((x) => x.seen >= 2) ?? act[0];
      if (w) out.push({ id: `${id}-wall`, instrumentId: id, kind: "wall", evidence: "walls", at: w.last_ts, title: `กำแพง ${w.side === "bid" ? "BID" : "ASK"}`,
        measured: `${w.size.toFixed(1)} ${ins.qtyUnit} ที่ ${w.price.toLocaleString("en-US")} ${ins.quote} · ${w.seen >= 2 ? `หน้านี้เห็นมา ${Math.max(0, Math.round(w.age_ms / 60000))} นาที` : "เพิ่งเห็นครั้งแรก"}`,
        explain: "คำสั่งรอซื้อ/ขายที่ใหญ่ผิดปกติเทียบกับระดับใกล้เคียง ยืนยันไม่ได้ว่าจะถูกจับคู่หรือถูกถอน" });
    }
    return out.slice(0, 3);
  }

  async alertHistory() {
    return []; // alerts are evaluated in the page from the same data; there is no stored history
  }
}
