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

/**
 * VWAP with volume-weighted standard deviation of typical price around it, from the anchor
 * (the desk's "Assist" bands: ±2σ and ±3σ). sd is null until there is volume.
 */
export function vwapBands(bars: Bar[], anchorT: number): ({ vwap: number; sd: number } | null)[] {
  let pv = 0, p2v = 0, vol = 0;
  return bars.map((b) => {
    if (b.t < anchorT) return null;
    const tp = (b.h + b.l + b.c) / 3;
    pv += tp * b.v;
    p2v += tp * tp * b.v;
    vol += b.v;
    if (vol <= 0) return null;
    const m = pv / vol;
    return { vwap: m, sd: Math.sqrt(Math.max(0, p2v / vol - m * m)) };
  });
}

/** 1, 2, 2.5 or 5 × a power of ten, the largest that is ≤ x (BTC 82,000 / 160 → 500, / 5000 → 10) */
export function niceStep(x: number): number {
  if (!(x > 0)) return 1;
  const mag = 10 ** Math.floor(Math.log10(x) + 1e-12);
  return [5, 2.5, 2, 1].map((m) => m * mag).find((s) => s <= x + 1e-12)!;
}

/** Round-number levels inside [lo, hi]: step ≈ price / 160 on the 1-2-2.5-5 series (BTC ~80,000 → 500,
 *  gold ~4,000 → 25), doubled until at most `max` levels fit. */
export function roundLevels(lo: number, hi: number, max = 24): { step: number; levels: number[] } {
  if (!(hi >= lo) || !(hi > 0)) return { step: 0, levels: [] };
  let step = niceStep(hi / 160);
  while ((hi - lo) / step > max) step *= 2;
  const levels: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) levels.push(Number(v.toFixed(10)));
  return { step, levels };
}

/**
 * The previous session (UTC day, 00:00 UTC = 07:00 UTC+7) before the newest bar: high, low and an
 * estimated POC / 70 % value area. null with a reason when the loaded bars do not cover ≥ 80 % of it.
 */
export function prevSession(bars: Bar[], bin: number): { hi: number; lo: number; poc: number | null; vaLow: number | null; vaHigh: number | null; start: number } | { reason: string } {
  if (bars.length < 2) return { reason: "ยังไม่มีแท่งพอ" };
  const step = Math.min(...bars.slice(1).map((b, i) => b.t - bars[i].t).filter((d) => d > 0));
  const today = sessionStart(bars[bars.length - 1].t);
  const start = today - 86400;
  const prev = bars.filter((b) => b.t >= start && b.t < today);
  if (prev.length * step < 0.8 * 86400) return { reason: "ข้อมูลที่โหลดไม่ครอบคลุมวันก่อนหน้า เลือกช่วง 3 วันเพื่อดู" };
  const p = profile(prev, bin);
  return { hi: Math.max(...prev.map((b) => b.h)), lo: Math.min(...prev.map((b) => b.l)), poc: p.poc, vaLow: p.vaLow, vaHigh: p.vaHigh, start };
}

/** Step-resample a sparse series (e.g. 5-minute OI) onto bar times: each bar gets the newest
 *  value at or before its close; bars before the first point get null. */
export function stepOnBars(bars: Bar[], points: { t: number; v: number }[], barSec: number): (number | null)[] {
  const pts = [...points].sort((a, z) => a.t - z.t);
  let j = -1;
  return bars.map((b) => {
    const close = b.t + barSec;
    while (j + 1 < pts.length && pts[j + 1].t <= close) j++;
    return j >= 0 && pts[j].t >= b.t - 3 * Math.max(barSec, 300) ? pts[j].v : null;
  });
}
