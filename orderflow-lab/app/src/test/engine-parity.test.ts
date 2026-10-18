// domain/engine.ts is a TypeScript port of web/engine.js (itself parity-tested against the Python
// engine by tests/test_js_parity.py). Run both on the same synthetic inputs and compare every output.
import { describe, expect, it } from "vitest";
import engineSrc from "../../../web/engine.js?raw";
import * as TS from "../domain/engine";

// web/engine.js is a UMD script: give it a CommonJS `module` and take its exports
const mod: { exports: Record<string, any> } = { exports: {} }; // eslint-disable-line @typescript-eslint/no-explicit-any
new Function("module", engineSrc)(mod);
const JS = mod.exports;

function rng(seed: number) {
  let s = seed >>> 0;
  return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
}

/** a synthetic L2 book around `mid` with a few oversized levels that drift between snapshots */
function book(ts: number, mid: number, tick: number, r: () => number, walls: [number, number][]): TS.Book {
  const bids: TS.Level[] = [], asks: TS.Level[] = [];
  for (let k = 1; k <= 260; k++) {
    bids.push([+(mid - k * tick).toFixed(6), +(0.5 + r() * 2).toFixed(3)]);
    asks.push([+(mid + k * tick).toFixed(6), +(0.5 + r() * 2).toFixed(3)]);
  }
  for (const [off, q] of walls) (off < 0 ? bids : asks)[Math.abs(off) - 1][1] = q;
  return { ts, exchange: "binance", symbol: "TEST", bids, asks };
}

function books(n: number, tick: number, seed: number): TS.Book[] {
  const r = rng(seed);
  let mid = 60000;
  return Array.from({ length: n }, (_, i) => {
    mid += (r() - 0.5) * tick * 6;
    const walls: [number, number][] = [[-12, 30 + i], [25, i % 4 === 3 ? 1 : 40], [-40 - (i % 3), 25], [8, i > 6 ? 60 : 2]];
    return book(1_700_000_000_000 + i * 10_000, +mid.toFixed(2), tick, r, walls);
  });
}

describe("engine.ts matches web/engine.js", () => {
  it("levelOf, median, percentile, threshold", () => {
    const r = rng(3);
    for (let i = 0; i < 200; i++) {
      const p = 1000 + r() * 70000, tick = [0.1, 0.5, 1, 10, 25][i % 5];
      expect(TS.levelOf(p, tick)).toBe(JS.levelOf(p, tick));
    }
    expect(TS.levelOf(2.5, 1)).toBe(JS.levelOf(2.5, 1)); // half-even
    const xs = Array.from({ length: 501 }, () => +(r() * 10).toFixed(3));
    expect(TS.median(xs)).toBe(JS.median(xs));
    expect(TS.median(xs.slice(1))).toBe(JS.median(xs.slice(1)));
    for (const pct of [0.5, 0.9, 0.99, 1]) expect(TS.percentile(xs, pct)).toBe(JS.percentile(xs, pct));
    const trades = xs.map((size, i) => ({ ts: i, price: 100, size, side: (i % 2 ? "buy" : "sell") as "buy" | "sell" }));
    expect(TS.threshold(trades, 0.99)).toBe(JS.threshold(trades, 0.99));
    expect(TS.bigTrades(trades, 9.5)).toEqual(JS.bigTrades(trades, 9.5));
  });

  it("detectWalls on raw and bucketed books, with and without merging", () => {
    for (const b of books(6, 0.5, 11)) {
      expect(TS.detectWalls(b)).toEqual(JS.detectWalls(b));
      expect(TS.detectWalls(b, 200, 4, 0, 0.5, 1)).toEqual(JS.detectWalls(b, 200, 4, 0, 0.5, 1));
      const bucketed = TS.bucketBook(b, 2);
      expect(TS.detectWalls(bucketed, 200, 3, 0, 0, 0.5)).toEqual(JS.detectWalls(bucketed, 200, 3, 0, 0, 0.5));
    }
  });

  it("WallTracker: statuses, ages, events with outcomes, bookLost", () => {
    const opts = { minMult: 4, maxLevels: 200, mergeGap: 0, persistMs: 60000, maxDistPct: 1.0, missLimit: 2, minSeen: 2 };
    const ts = new TS.WallTracker(0.5, opts), js = new JS.WallTracker(0.5, opts);
    for (const b of books(14, 0.5, 5)) {
      expect(ts.update(b)).toEqual(js.update(b));
      expect(ts.active()).toEqual(js.active());
    }
    expect(ts.events(50)).toEqual(js.events(50));
    expect(ts.events(50).map((e) => e.outcome)).toContain("removed");
    expect(ts.bookLost(1_800_000_000_000)).toEqual(js.bookLost(1_800_000_000_000));
    expect(ts.active()).toEqual([]);
  });

  it("profileFromBars, poc, valueArea", () => {
    const r = rng(9);
    let c = 2400;
    const bars = Array.from({ length: 300 }, (_, i) => {
      const o = c; c = +(c + (r() - 0.5) * 2).toFixed(2);
      return { t: i * 60, o, h: Math.max(o, c) + +(r()).toFixed(2), l: Math.min(o, c) - +(r()).toFixed(2), c, v: +(r() * 50).toFixed(3) };
    });
    for (const tick of [0.5, 0.1, 1]) {
      const a = TS.profileFromBars(bars, tick), b = JS.profileFromBars(bars, tick);
      expect(a.sortedLevels()).toEqual(b.sortedLevels());
      expect(TS.poc(a)).toBe(JS.poc(b));
      expect(TS.valueArea(a, 0.7)).toEqual(JS.valueArea(b, 0.7));
    }
  });
});
