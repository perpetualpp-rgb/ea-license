/* Order Flow Lab engine — JavaScript port of orderflow-lab/engine/*.py.
 *
 * Keep this in lock-step with the Python modules: tests/test_js_parity.py runs
 * both on the same input and compares the numbers. Records use the same shapes
 * as feeds/types.py:
 *   trade = {ts, price, size, side:"buy"|"sell", exchange, symbol, trade_id}   (side = taker)
 *   book  = {ts, exchange, symbol, bids:[[price,size],...] desc, asks:[[price,size],...] asc}
 *   bar   = {t (unix s), o, h, l, c, v}
 */
(function (root, factory) {
  const OF = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = OF;
  root.OF = OF;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // ---- numeric helpers that mirror Python semantics --------------------------
  function roundHalfEven(x) {
    const f = Math.floor(x), d = x - f;
    if (d < 0.5) return f;
    if (d > 0.5) return f + 1;
    return f % 2 === 0 ? f : f + 1;
  }
  function exp10(x) { let e = 0; while (x < 1 && e > -12) { x *= 10; e -= 1; } return e; }
  function levelOf(price, tick) {
    const digits = Math.max(0, -exp10(tick)) + 2;
    return Number((roundHalfEven(price / tick) * tick).toFixed(digits));
  }
  function median(values) {
    const s = [...values].sort((a, b) => a - b), n = s.length;
    if (!n) return 0;
    return n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2;
  }
  function signedSize(t) { return t.side === "buy" ? t.size : -t.size; }
  function bucketStart(ts, interval) { return ts - (ts % interval); }
  function bestBid(book) { return book.bids.length ? book.bids[0][0] : null; }
  function bestAsk(book) { return book.asks.length ? book.asks[0][0] : null; }
  function bookMid(book) {
    if (book.bids.length && book.asks.length) return (book.bids[0][0] + book.asks[0][0]) / 2;
    return bestBid(book) ?? bestAsk(book);
  }
  function sortBook(bids, asks) {
    return {
      bids: bids.filter(l => l[1] > 0).sort((a, b) => b[0] - a[0]),
      asks: asks.filter(l => l[1] > 0).sort((a, b) => a[0] - b[0]),
    };
  }

  // ---- bars ------------------------------------------------------------------
  function barsFromTrades(trades, intervalMs) {
    const bars = [];
    let cur = null;
    const finish = c => ({ t: Math.floor(c.t / 1000), o: c.o, h: c.h, l: c.l, c: c.c, v: c.v });
    for (const tr of trades) {
      const b0 = bucketStart(tr.ts, intervalMs);
      if (!cur || b0 !== cur.t) {
        if (cur) bars.push(finish(cur));
        cur = { t: b0, o: tr.price, h: tr.price, l: tr.price, c: tr.price, v: 0 };
      }
      cur.h = Math.max(cur.h, tr.price);
      cur.l = Math.min(cur.l, tr.price);
      cur.c = tr.price;
      cur.v += tr.size;
    }
    if (cur) bars.push(finish(cur));
    return bars;
  }

  // ---- cvd -------------------------------------------------------------------
  function cvdSeries(trades, start = 0) {
    const out = []; let acc = start;
    for (const tr of trades) { acc += signedSize(tr); out.push([tr.ts, acc]); }
    return out;
  }
  function deltaBuckets(trades, intervalMs) {
    const out = []; let cur = null;
    for (const tr of trades) {
      const b0 = Math.floor(bucketStart(tr.ts, intervalMs) / 1000);
      if (!cur || cur.t !== b0) { cur = { t: b0, buy: 0, sell: 0, delta: 0, n: 0 }; out.push(cur); }
      if (tr.side === "buy") cur.buy += tr.size; else cur.sell += tr.size;
      cur.delta = cur.buy - cur.sell;
      cur.n += 1;
    }
    return out;
  }
  function cvdFromBuckets(buckets, start = 0) {
    const out = []; let acc = start;
    for (const b of buckets) { acc += b.delta; out.push([b.t, acc]); }
    return out;
  }
  function approxBarDelta(bar) {
    const rng = bar.h - bar.l;
    if (rng <= 0 || bar.v <= 0) return 0;
    return bar.v * ((bar.c - bar.l) - (bar.h - bar.c)) / rng;
  }
  function approxCvdFromBars(bars, start = 0) {
    const out = []; let acc = start;
    for (const b of bars) { acc += approxBarDelta(b); out.push([b.t, acc]); }
    return out;
  }
  function divergence(prices, cvds, lookback = 20) {
    const n = Math.min(prices.length, cvds.length);
    if (n < lookback + 1) return null;
    const pNow = prices[n - 1], cNow = cvds[n - 1];
    const pPrev = prices.slice(n - 1 - lookback, n - 1), cPrev = cvds.slice(n - 1 - lookback, n - 1);
    if (pNow > Math.max(...pPrev) && cNow < Math.max(...cPrev)) return "bearish";
    if (pNow < Math.min(...pPrev) && cNow > Math.min(...cPrev)) return "bullish";
    return null;
  }

  // ---- profile ---------------------------------------------------------------
  class Profile {
    constructor(tick, approximate = false) { this.tick = tick; this.approximate = approximate; this.levels = new Map(); }
    add(price, size, side) {
      const k = levelOf(price, this.tick);
      let lv = this.levels.get(k);
      if (!lv) { lv = { buy: 0, sell: 0, total: 0 }; this.levels.set(k, lv); }
      if (side === "buy") lv.buy += size; else if (side === "sell") lv.sell += size;
      lv.total += size;
    }
    get total() { let s = 0; for (const v of this.levels.values()) s += v.total; return s; }
    sortedLevels() { return [...this.levels.entries()].sort((a, b) => a[0] - b[0]); }
    rows() { return this.sortedLevels().map(([price, v]) => ({ price, ...v })); }
  }
  function profileFromTrades(trades, tick) { const p = new Profile(tick); for (const t of trades) p.add(t.price, t.size, t.side); return p; }
  function profileFromBars(bars, tick) {
    const p = new Profile(tick, true);
    for (const b of bars) {
      const lo = levelOf(b.l, tick), hi = levelOf(b.h, tick);
      const n = Math.round((hi - lo) / tick) + 1, share = b.v / n;
      for (let i = 0; i < n; i++) p.add(lo + i * tick, share, null);
    }
    return p;
  }
  function poc(prof) {
    if (!prof.levels.size) return null;
    let best = null, bp = null;
    for (const [p, v] of prof.levels) if (best === null || v.total > best || (v.total === best && p < bp)) { best = v.total; bp = p; }
    return bp;
  }
  function valueArea(prof, pct = 0.70) {
    const levels = prof.sortedLevels();
    if (!levels.length) return null;
    const prices = levels.map(x => x[0]), vols = levels.map(x => x[1].total);
    let i = vols.indexOf(Math.max(...vols)), lo = i, hi = i, acc = vols[i];
    const target = prof.total * pct;
    while (acc < target && (lo > 0 || hi < vols.length - 1)) {
      const down = lo > 0 ? vols[lo - 1] : -1, up = hi < vols.length - 1 ? vols[hi + 1] : -1;
      if (up > down) { hi += 1; acc += vols[hi]; } else { lo -= 1; acc += vols[lo]; }
    }
    return [prices[lo], prices[hi]];
  }
  function sessionRange(prof, pct = 0.70) {
    if (!prof.levels.size) return null;
    const levels = prof.sortedLevels(), va = valueArea(prof, pct);
    return { high: levels[levels.length - 1][0], low: levels[0][0], poc: poc(prof), va_low: va[0], va_high: va[1],
             va_pct: pct, total: prof.total, approximate: prof.approximate };
  }
  function nodes(prof, top = 3) {
    const levels = prof.sortedLevels();
    if (!levels.length) return { hvn: [], lvn: [] };
    const lo = levels[0][0], hi = levels[levels.length - 1][0], n = Math.round((hi - lo) / prof.tick) + 1;
    const full = [];
    for (let i = 0; i < n; i++) { const p = levelOf(lo + i * prof.tick, prof.tick); full.push([p, (prof.levels.get(p) || { total: 0 }).total]); }
    const hvn = [...full].sort((a, b) => b[1] - a[1]).slice(0, top), lvn = [...full].sort((a, b) => a[1] - b[1]).slice(0, top);
    return { hvn: hvn.map(([price, total]) => ({ price, total })), lvn: lvn.map(([price, total]) => ({ price, total })) };
  }

  // ---- vwap ------------------------------------------------------------------
  function vwapBands(bars, sigmas = [1, 2, 3]) {
    const out = []; let sV = 0, sPV = 0, sP2V = 0;
    for (const b of bars) {
      const p = (b.h + b.l + b.c) / 3, v = b.v;
      sV += v; sPV += p * v; sP2V += p * p * v;
      if (sV <= 0) continue;
      const vwap = sPV / sV, sd = Math.sqrt(Math.max(0, sP2V / sV - vwap * vwap));
      const bands = {}; for (const k of sigmas) bands[k] = [vwap - k * sd, vwap + k * sd];
      out.push({ t: b.t, vwap, sd, bands });
    }
    return out;
  }
  function position(price, point) {
    const z = point.sd > 0 ? (price - point.vwap) / point.sd : 0;
    const label = z >= 2 ? "expensive" : z <= -2 ? "cheap" : z > 0 ? "above" : z < 0 ? "below" : "at_vwap";
    return { z, label };
  }
  function bandShape(series, lookback = 12, expand = 1.25, flat = 1.05) {
    if (series.length <= lookback) return null;
    const now = series[series.length - 1].sd, then = series[series.length - 1 - lookback].sd;
    if (then <= 0) return null;
    const r = now / then;
    return r >= expand ? "expanding" : r <= flat ? "flat" : "widening";
  }

  // ---- walls -----------------------------------------------------------------
  function detectWalls(book, maxLevels = 200, minMult = 4.0, minSize = 0, mergeGap = 0, maxDistPct = 1.0) {
    const mid = bookMid(book) || 0, out = [];
    for (const [side, all] of [["bid", book.bids.slice(0, maxLevels)], ["ask", book.asks.slice(0, maxLevels)]]) {
      let levels = all;
      if (mid && maxDistPct > 0) levels = levels.filter(lv => Math.abs(lv[0] - mid) / mid * 100 <= maxDistPct);
      const sizes = levels.filter(l => l[1] > 0).map(l => l[1]);
      if (!sizes.length) continue;
      const med = median(sizes);
      const cands = levels.filter(([, s]) => s >= Math.max(minSize, minMult * med))
        .map(([price, size]) => ({ side, price, size, mult: med > 0 ? size / med : 0 }));
      const merged = [];
      for (const w of cands) {
        const m = merged[merged.length - 1];
        if (m && mergeGap > 0 && Math.abs(w.price - m.edge) <= mergeGap * (1 + 1e-6)) {
          m.size += w.size; m.levels += 1; m.edge = w.price;
          if (w.size > m.peak) { m.peak = w.size; m.price = w.price; }
        } else merged.push({ ...w, levels: 1, edge: w.price, peak: w.size });
      }
      for (const m of merged) {
        delete m.edge; delete m.peak;
        m.dist = Math.abs(m.price - mid);
        m.dist_pct = mid ? m.dist / mid * 100 : 0;
        out.push(m);
      }
    }
    out.sort((a, b) => b.size - a.size);
    return out;
  }

  class WallTracker {
    constructor(tick, opts = {}) {
      Object.assign(this, { minMult: 4.0, maxLevels: 200, minSize: 0, mergeGap: 0, persistMs: 60000, missLimit: 2,
                            changePct: 0.25, maxDistPct: 1.0, minSeen: 2 }, opts);
      this.tick = tick; this.walls = new Map(); this._events = []; this.lastBook = null;
    }
    update(book) {
      const seen = new Set();
      for (const w of detectWalls(book, this.maxLevels, this.minMult, this.minSize, this.mergeGap, this.maxDistPct)) {
        const key = w.side + ":" + levelOf(w.price, this.tick);
        seen.add(key);
        const rec = this.walls.get(key);
        if (!rec) {
          this.walls.set(key, { ...w, first_ts: book.ts, last_ts: book.ts, size_first: w.size, size_prev: w.size, seen: 1, misses: 0, status: "new" });
          continue;
        }
        const prev = rec.size, change = prev > 0 ? (w.size - prev) / prev : 0;
        Object.assign(rec, w, { last_ts: book.ts, size_prev: prev, seen: rec.seen + 1, misses: 0,
          status: change >= this.changePct ? "adding" : change <= -this.changePct ? "pulling" : "holding" });
      }
      const events = [];
      for (const [key, rec] of [...this.walls.entries()]) {
        if (seen.has(key)) continue;
        rec.misses += 1;
        if (rec.misses > this.missLimit) {
          this.walls.delete(key);
          if (rec.seen < this.minSeen) continue;
          const ev = { ...rec, outcome: this._outcome(rec, book), ended_ts: book.ts, age_ms: rec.last_ts - rec.first_ts };
          this._events.push(ev); events.push(ev);
        }
      }
      this.lastBook = book;
      return events;
    }
    _outcome(rec, book) {
      const touched = rec.side === "bid" ? (bestBid(book) !== null && bestBid(book) <= rec.price)
                                         : (bestAsk(book) !== null && bestAsk(book) >= rec.price);
      return touched ? "filled" : "pulled";
    }
    active(nowTs) {
      const now = nowTs ?? (this.lastBook ? this.lastBook.ts : 0);
      const rows = [...this.walls.values()].map(rec => {
        const age = now - rec.first_ts;
        return { ...rec, age_ms: age, persistent: age >= this.persistMs, growth: rec.size_first ? rec.size / rec.size_first - 1 : 0 };
      });
      rows.sort((a, b) => b.size - a.size);
      return rows;
    }
    events(n = 20) { return this._events.slice(-n); }
  }

  // ---- big trades ------------------------------------------------------------
  function percentile(values, pct) {
    if (!values.length) return 0;
    const s = [...values].sort((a, b) => a - b);
    const k = Math.max(0, Math.min(s.length - 1, Math.ceil(pct * s.length) - 1));
    return s[k];
  }
  function threshold(trades, pct = 0.99, minAbs = 0) { return Math.max(minAbs, percentile(trades.map(t => t.size), pct)); }
  function bigTrades(trades, thr) { return trades.filter(t => thr > 0 && t.size >= thr); }
  function zones(big, tick, contestRatio = 0.5) {
    const acc = new Map();
    for (const t of big) {
      const k = levelOf(t.price, tick);
      let z = acc.get(k);
      if (!z) { z = { price: k, buy: 0, sell: 0, n: 0, first_ts: t.ts, last_ts: t.ts }; acc.set(k, z); }
      if (t.side === "buy") z.buy += t.size; else z.sell += t.size;
      z.n += 1; z.first_ts = Math.min(z.first_ts, t.ts); z.last_ts = Math.max(z.last_ts, t.ts);
    }
    const rows = [...acc.values()].map(z => {
      const bigSide = Math.max(z.buy, z.sell), smallSide = Math.min(z.buy, z.sell);
      return { ...z, total: z.buy + z.sell, delta: z.buy - z.sell, contested: bigSide > 0 && smallSide / bigSide >= contestRatio };
    });
    rows.sort((a, b) => b.total - a.total);
    return rows;
  }
  function recent(big, nowMs, windowMs) { return big.filter(t => nowMs - t.ts <= windowMs); }

  // ---- round levels ----------------------------------------------------------
  function roundLevels(low, high, step) {
    const out = []; let x = Math.ceil(low / step) * step;
    while (x <= high + 1e-9) { out.push(Number(x.toFixed(8))); x += step; }
    return out;
  }
  function nearest(price, step) {
    const below = Math.floor(price / step) * step, above = below < price ? below + step : below;
    return [Number(below.toFixed(8)), Number(above.toFixed(8))];
  }

  // ---- summaries -------------------------------------------------------------
  function summarizeBars(bars, tick, roundStep, vaPct = 0.70, magnetPct = 0.30) {
    if (!bars.length) return { error: "no bars" };
    const prof = profileFromBars(bars, tick), rng = sessionRange(prof, vaPct), magnet = valueArea(prof, magnetPct);
    const bands = vwapBands(bars), last = bars[bars.length - 1], cvds = approxCvdFromBars(bars);
    const lastBand = bands[bands.length - 1];
    return {
      mode: "bars", approximate: true, bars: bars.length, bar_list: bars, t_first: bars[0].t, t_last: last.t,
      last: last.c, volume: bars.reduce((s, b) => s + b.v, 0),
      range: rng, magnet: { low: magnet[0], high: magnet[1], pct: magnetPct }, nodes: nodes(prof, 3),
      vwap: lastBand ? { ...lastBand, position: position(last.c, lastBand), shape: bandShape(bands) } : null,
      vwap_series: bands, profile: prof,
      cvd: { value: cvds[cvds.length - 1][1], approximate: true, divergence: divergence(bars.map(b => b.c), cvds.map(c => c[1])), series: cvds },
      round_levels: roundLevels(rng.low, rng.high, roundStep), nearest_round: nearest(last.c, roundStep),
    };
  }

  function summarizeTrades(trades, books, tick, roundStep, intervalMs = 60000, vaPct = 0.70, magnetPct = 0.30,
                           bigPct = 0.99, bigMin = 0, wallMult = 4.0, wallDepth = 200, persistMs = 60000, wallMaxDistPct = 1.0,
                           tracker = null) {
    if (!trades.length) return { error: "no trades" };
    const bars = barsFromTrades(trades, intervalMs);
    const out = summarizeBars(bars, tick, roundStep, vaPct, magnetPct);
    Object.assign(out, { mode: "trades", approximate: false, trades: trades.length, last: trades[trades.length - 1].price,
                         ts_first: trades[0].ts, ts_last: trades[trades.length - 1].ts });
    const prof = profileFromTrades(trades, tick);
    out.range = sessionRange(prof, vaPct);
    const magnet = valueArea(prof, magnetPct);
    out.magnet = { low: magnet[0], high: magnet[1], pct: magnetPct };
    out.nodes = nodes(prof, 3);
    out.profile = prof;
    let pb = 0, ps = 0; for (const v of prof.levels.values()) { pb += v.buy; ps += v.sell; }
    out.profile_buy = pb; out.profile_sell = ps;

    const buckets = deltaBuckets(trades, intervalMs), series = cvdFromBuckets(buckets);
    out.cvd = { value: series[series.length - 1][1], approximate: false, last_buckets: buckets.slice(-5), buckets,
                series, divergence: divergence(bars.map(b => b.c), series.map(c => c[1])) };

    const thr = threshold(trades, bigPct, bigMin), big = bigTrades(trades, thr);
    out.big_trades = { threshold: thr, pct: bigPct, count: big.length, zones: zones(big, tick).slice(0, 8), list: big,
                       recent: big.slice(-5).map(t => ({ ts: t.ts, price: t.price, size: t.size, side: t.side })) };

    const tr = tracker || new WallTracker(tick, { minMult: wallMult, maxLevels: wallDepth, mergeGap: tick, persistMs, maxDistPct: wallMaxDistPct });
    if (!tracker) for (const b of books) tr.update(b);
    const active = tr.active();
    const keep = ["side", "price", "size", "mult", "levels", "dist", "dist_pct", "status", "seen", "age_ms", "persistent", "growth"];
    const pick = (w) => Object.fromEntries(keep.map(k => [k, w[k]]));
    const lastBook = tr.lastBook || books[books.length - 1] || null;
    out.walls = {
      snapshots: books.length,
      bid: active.filter(w => w.side === "bid").slice(0, 5).map(pick),
      ask: active.filter(w => w.side === "ask").slice(0, 5).map(pick),
      events: tr.events(10).map(e => ({ side: e.side, price: e.price, size: e.size, outcome: e.outcome, age_ms: e.age_ms, ended_ts: e.ended_ts })),
      book_ts: lastBook ? lastBook.ts : null, best_bid: lastBook ? bestBid(lastBook) : null, best_ask: lastBook ? bestAsk(lastBook) : null,
    };
    return out;
  }

  return { levelOf, median, signedSize, bucketStart, bestBid, bestAsk, bookMid, sortBook, barsFromTrades,
           cvdSeries, deltaBuckets, cvdFromBuckets, approxBarDelta, approxCvdFromBars, divergence,
           Profile, profileFromTrades, profileFromBars, poc, valueArea, sessionRange, nodes,
           vwapBands, position, bandShape, detectWalls, WallTracker,
           percentile, threshold, bigTrades, zones, recent, roundLevels, nearest, summarizeBars, summarizeTrades };
});
