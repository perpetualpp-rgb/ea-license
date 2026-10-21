// TypeScript port of the parts of web/engine.js (itself a port of engine/*.py) that the owner's
// connector source needs: order-book walls (detect + tracker), big-trade threshold and the bar
// volume profile. Same formulas, same defaults, same tie-breaks — src/test/engine-parity.test.ts
// runs web/engine.js and this file on the same inputs and compares the outputs.
// Shapes follow web/engine.js: book = {ts, bids:[[price,size]] desc, asks:[[price,size]] asc}.

export type Level = [number, number];
export interface Book { ts: number; bids: Level[]; asks: Level[]; exchange?: string; symbol?: string }
export interface Trade { ts: number; price: number; size: number; side: "buy" | "sell" }

// ---- numeric helpers that mirror Python semantics ------------------------------------------
function roundHalfEven(x: number): number {
  const f = Math.floor(x), d = x - f;
  if (d < 0.5) return f;
  if (d > 0.5) return f + 1;
  return f % 2 === 0 ? f : f + 1;
}
function exp10(x: number): number { let e = 0; while (x < 1 && e > -12) { x *= 10; e -= 1; } return e; }
export function levelOf(price: number, tick: number): number {
  const digits = Math.max(0, -exp10(tick)) + 2;
  return Number((roundHalfEven(price / tick) * tick).toFixed(digits));
}
export function median(values: number[]): number {
  const s = [...values].sort((a, b) => a - b), n = s.length;
  if (!n) return 0;
  return n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2;
}
export const bestBid = (book: Book): number | null => (book.bids.length ? book.bids[0][0] : null);
export const bestAsk = (book: Book): number | null => (book.asks.length ? book.asks[0][0] : null);
export function bookMid(book: Book): number | null {
  if (book.bids.length && book.asks.length) return (book.bids[0][0] + book.asks[0][0]) / 2;
  return bestBid(book) ?? bestAsk(book);
}
export function sortBook(bids: Level[], asks: Level[]): { bids: Level[]; asks: Level[] } {
  return {
    bids: bids.filter((l) => l[1] > 0).sort((a, b) => b[0] - a[0]),
    asks: asks.filter((l) => l[1] > 0).sort((a, b) => a[0] - b[0]),
  };
}

/** desk/template.html bucketBook: bids floor and asks ceil to `b`, sizes summed per bucket */
export function bucketBook(book: Book, b: number): Book {
  const bid = new Map<number, number>(), ask = new Map<number, number>(), key = (v: number) => +v.toFixed(6);
  for (const [p, q] of book.bids) { const k = key(Math.floor(p / b + 1e-9) * b); bid.set(k, (bid.get(k) || 0) + q); }
  for (const [p, q] of book.asks) { const k = key(Math.ceil(p / b - 1e-9) * b); ask.set(k, (ask.get(k) || 0) + q); }
  return { ts: book.ts, exchange: "binance", symbol: book.symbol, bids: [...bid].sort((x, y) => y[0] - x[0]), asks: [...ask].sort((x, y) => x[0] - y[0]) };
}

// ---- walls ---------------------------------------------------------------------------------
export interface Wall { side: "bid" | "ask"; price: number; size: number; mult: number; levels: number; dist: number; dist_pct: number }

export function detectWalls(book: Book, maxLevels = 200, minMult = 4.0, minSize = 0, mergeGap = 0, maxDistPct = 1.0): Wall[] {
  const mid = bookMid(book) || 0, out: Wall[] = [];
  for (const [side, all] of [["bid", book.bids.slice(0, maxLevels)], ["ask", book.asks.slice(0, maxLevels)]] as const) {
    let levels = all;
    if (mid && maxDistPct > 0) levels = levels.filter((lv) => Math.abs(lv[0] - mid) / mid * 100 <= maxDistPct);
    const sizes = levels.filter((l) => l[1] > 0).map((l) => l[1]);
    if (!sizes.length) continue;
    const med = median(sizes);
    const cands = levels.filter(([, s]) => s >= Math.max(minSize, minMult * med))
      .map(([price, size]) => ({ side, price, size, mult: med > 0 ? size / med : 0 }));
    const merged: { side: "bid" | "ask"; price: number; size: number; mult: number; levels: number; edge: number; peak: number }[] = [];
    for (const w of cands) {
      const m = merged[merged.length - 1];
      if (m && mergeGap > 0 && Math.abs(w.price - m.edge) <= mergeGap * (1 + 1e-6)) {
        m.size += w.size; m.levels += 1; m.edge = w.price;
        if (w.size > m.peak) { m.peak = w.size; m.price = w.price; }
      } else merged.push({ ...w, levels: 1, edge: w.price, peak: w.size });
    }
    for (const { edge: _e, peak: _p, ...m } of merged) {
      const dist = Math.abs(m.price - mid);
      out.push({ ...m, dist, dist_pct: mid ? dist / mid * 100 : 0 });
    }
  }
  out.sort((a, b) => b.size - a.size);
  return out;
}

export type WallStatus = "new" | "holding" | "adding" | "pulling";
export interface WallRecord extends Wall {
  first_ts: number; last_ts: number; size_first: number; size_prev: number; seen: number; misses: number; status: WallStatus;
}
export interface WallEnd extends WallRecord { outcome: "price_reached" | "removed" | "unknown"; ended_ts: number; age_ms: number }
export interface ActiveWall extends WallRecord { age_ms: number; persistent: boolean; growth: number }

export interface TrackerOptions {
  minMult: number; maxLevels: number; minSize: number; mergeGap: number; persistMs: number; missLimit: number;
  changePct: number; maxDistPct: number; minSeen: number;
}

export class WallTracker implements TrackerOptions {
  minMult = 4.0; maxLevels = 200; minSize = 0; mergeGap = 0; persistMs = 60000; missLimit = 2;
  changePct = 0.25; maxDistPct = 1.0; minSeen = 2;
  walls = new Map<string, WallRecord>();
  lastBook: Book | null = null;
  private _events: WallEnd[] = [];

  constructor(public tick: number, opts: Partial<TrackerOptions> = {}) {
    Object.assign(this, opts);
  }

  update(book: Book): WallEnd[] {
    const seen = new Set<string>();
    for (const w of detectWalls(book, this.maxLevels, this.minMult, this.minSize, this.mergeGap, this.maxDistPct)) {
      const key = w.side + ":" + levelOf(w.price, this.tick);
      seen.add(key);
      const rec = this.walls.get(key);
      if (!rec) {
        this.walls.set(key, { ...w, first_ts: book.ts, last_ts: book.ts, size_first: w.size, size_prev: w.size, seen: 1, misses: 0, status: "new" });
        continue;
      }
      const prev = rec.size, change = prev > 0 ? (w.size - prev) / prev : 0;
      Object.assign(rec, w, {
        last_ts: book.ts, size_prev: prev, seen: rec.seen + 1, misses: 0,
        status: change >= this.changePct ? "adding" : change <= -this.changePct ? "pulling" : "holding",
      });
    }
    const events: WallEnd[] = [];
    for (const [key, rec] of [...this.walls.entries()]) {
      if (seen.has(key)) continue;
      rec.misses += 1;
      if (rec.misses > this.missLimit) {
        this.walls.delete(key);
        if (rec.seen < this.minSeen) continue;
        events.push(this._end(rec, this._outcome(rec, book), book.ts));
      }
    }
    this.lastBook = book;
    return events;
  }

  /** the book feed broke: what happened to the walls meanwhile cannot be seen */
  bookLost(ts: number): WallEnd[] {
    const ended = [...this.walls.values()].filter((rec) => rec.seen >= this.minSeen).map((rec) => this._end(rec, "unknown", ts));
    this.walls.clear();
    return ended;
  }

  private _end(rec: WallRecord, outcome: WallEnd["outcome"], ts: number): WallEnd {
    const ev: WallEnd = { ...rec, outcome, ended_ts: ts, age_ms: rec.last_ts - rec.first_ts };
    this._events.push(ev);
    return ev;
  }

  /** what the L2 book showed at removal, never whether the size was executed or cancelled */
  private _outcome(rec: WallRecord, book: Book): WallEnd["outcome"] {
    const best = rec.side === "bid" ? bestBid(book) : bestAsk(book);
    if (best === null) return "unknown";
    return (rec.side === "bid" ? best <= rec.price : best >= rec.price) ? "price_reached" : "removed";
  }

  active(nowTs?: number): ActiveWall[] {
    const now = nowTs ?? (this.lastBook ? this.lastBook.ts : 0);
    const rows = [...this.walls.values()].map((rec) => {
      const age = now - rec.first_ts;
      return { ...rec, age_ms: age, persistent: age >= this.persistMs, growth: rec.size_first ? rec.size / rec.size_first - 1 : 0 };
    });
    rows.sort((a, b) => b.size - a.size);
    return rows;
  }

  events(n = 20): WallEnd[] { return this._events.slice(-n); }
}

// ---- big trades ----------------------------------------------------------------------------
export function percentile(values: number[], pct: number): number {
  if (!values.length) return 0;
  const s = [...values].sort((a, b) => a - b);
  const k = Math.max(0, Math.min(s.length - 1, Math.ceil(pct * s.length) - 1));
  return s[k];
}
export const threshold = (trades: Trade[], pct = 0.99, minAbs = 0) => Math.max(minAbs, percentile(trades.map((t) => t.size), pct));
export const bigTrades = <T extends Trade>(trades: T[], thr: number): T[] => trades.filter((t) => thr > 0 && t.size >= thr);

// ---- profile -------------------------------------------------------------------------------
export class Profile {
  levels = new Map<number, { buy: number; sell: number; total: number }>();
  constructor(public tick: number, public approximate = false) {}
  add(price: number, size: number, side: "buy" | "sell" | null) {
    const k = levelOf(price, this.tick);
    let lv = this.levels.get(k);
    if (!lv) { lv = { buy: 0, sell: 0, total: 0 }; this.levels.set(k, lv); }
    if (side === "buy") lv.buy += size; else if (side === "sell") lv.sell += size;
    lv.total += size;
  }
  get total() { let s = 0; for (const v of this.levels.values()) s += v.total; return s; }
  sortedLevels() { return [...this.levels.entries()].sort((a, b) => a[0] - b[0]); }
}

/** each bar's volume spread evenly over its high–low levels: an estimate, not volume at price */
export function profileFromBars(bars: { h: number; l: number; v: number }[], tick: number): Profile {
  const p = new Profile(tick, true);
  for (const b of bars) {
    const lo = levelOf(b.l, tick), hi = levelOf(b.h, tick);
    const n = Math.round((hi - lo) / tick) + 1, share = b.v / n;
    for (let i = 0; i < n; i++) p.add(lo + i * tick, share, null);
  }
  return p;
}
export function poc(prof: Profile): number | null {
  if (!prof.levels.size) return null;
  let best: number | null = null, bp: number | null = null;
  for (const [p, v] of prof.levels) if (best === null || v.total > best || (v.total === best && p < (bp as number))) { best = v.total; bp = p; }
  return bp;
}
export function valueArea(prof: Profile, pct = 0.70): [number, number] | null {
  const levels = prof.sortedLevels();
  if (!levels.length) return null;
  const prices = levels.map((x) => x[0]), vols = levels.map((x) => x[1].total);
  const i = vols.indexOf(Math.max(...vols));
  let lo = i, hi = i, acc = vols[i];
  const target = prof.total * pct;
  while (acc < target && (lo > 0 || hi < vols.length - 1)) {
    const down = lo > 0 ? vols[lo - 1] : -1, up = hi < vols.length - 1 ? vols[hi + 1] : -1;
    if (up > down) { hi += 1; acc += vols[hi]; } else { lo -= 1; acc += vols[lo]; }
  }
  return [prices[lo], prices[hi]];
}
