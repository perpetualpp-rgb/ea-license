// Indicator maths used by the analysis screen. Each function says what it measures and how.
import type { Bar } from "../data/types";

export function aggregate(bars: Bar[], sec: number): Bar[] {
  const out: Bar[] = [];
  for (const b of bars) {
    const t = Math.floor(b.t / sec) * sec;
    const cur = out[out.length - 1];
    if (!cur || cur.t !== t) {
      out.push({ ...b, t });
      continue;
    }
    cur.h = Math.max(cur.h, b.h);
    cur.l = Math.min(cur.l, b.l);
    cur.c = b.c;
    cur.v += b.v;
    cur.buyV = cur.buyV == null || b.buyV == null ? null : cur.buyV + b.buyV;
  }
  return out;
}

/** taker buy − taker sell for one bar; null when the source has no aggressor split */
export const barDelta = (b: Bar): number | null => (b.buyV == null ? null : b.buyV - (b.v - b.buyV));

/**
 * CVD accumulated from `anchorT` (unix s). Bars before the anchor are null so the
 * line starts where the session starts. Returns null values when the split is unknown,
 * rather than inventing a number.
 */
export function cvd(bars: Bar[], anchorT: number): (number | null)[] {
  let acc = 0;
  let known = true;
  return bars.map((b) => {
    if (b.t < anchorT) return null;
    const d = barDelta(b);
    if (d == null) known = false;
    acc += d ?? 0;
    return known ? acc : null;
  });
}

/** VWAP from each bar's typical price (h+l+c)/3 weighted by volume, from the anchor. */
export function vwap(bars: Bar[], anchorT: number): (number | null)[] {
  let pv = 0;
  let vol = 0;
  return bars.map((b) => {
    if (b.t < anchorT) return null;
    pv += ((b.h + b.l + b.c) / 3) * b.v;
    vol += b.v;
    return vol > 0 ? pv / vol : null;
  });
}

/**
 * Volume profile estimated by spreading each bar's volume evenly across its high–low range.
 * This is an approximation, not executed volume at price.
 */
export function profile(bars: Bar[], bin: number, vaPct = 0.7) {
  const m = new Map<number, number>();
  for (const b of bars) {
    const lo = Math.floor(b.l / bin);
    const hi = Math.floor(b.h / bin);
    const n = hi - lo + 1;
    for (let k = lo; k <= hi; k++) m.set(k, (m.get(k) ?? 0) + b.v / n);
  }
  if (!m.size) return { poc: null, vaLow: null, vaHigh: null, levels: [] as { price: number; vol: number }[] };
  const keys = [...m.keys()].sort((a, z) => a - z);
  const vols = keys.map((k) => m.get(k)!);
  let i = vols.indexOf(Math.max(...vols));
  const pocIdx = i;
  let lo = i;
  let hi = i;
  let acc = vols[i];
  const total = vols.reduce((a, v) => a + v, 0);
  while (acc < total * vaPct && (lo > 0 || hi < vols.length - 1)) {
    const down = lo > 0 ? vols[lo - 1] : -1;
    const up = hi < vols.length - 1 ? vols[hi + 1] : -1;
    if (up > down) acc += vols[++hi];
    else acc += vols[--lo];
  }
  i = pocIdx;
  const mid = (k: number) => (k + 0.5) * bin;
  return { poc: mid(keys[i]), vaLow: keys[lo] * bin, vaHigh: (keys[hi] + 1) * bin, levels: keys.map((k, j) => ({ price: mid(k), vol: vols[j] })) };
}

/** start of the current CVD/VWAP session: 00:00 UTC = 07:00 Asia/Bangkok */
export function sessionStart(nowSec: number): number {
  return Math.floor(nowSec / 86400) * 86400;
}
