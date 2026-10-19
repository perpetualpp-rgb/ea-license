/* Order Flow Lab desk plan — the pure rules behind the "แผน" table: levels, zones, entry/SL/TP and bias.
 *
 * desk/build.py inlines this file into the page (window.OFP) after engine.js (OF) and journal.js (OFJ);
 * desk/backtest.js runs the same file under node over historical candles. Nothing here reads page state:
 * the page passes what it has (order-book walls, big-trade zones, live buy pressure, the OI read) in ctx.
 *
 *   bar = {t (unix s, bar start), o, h, l, c, v, bv}   bv = taker-buy volume, NaN when unknown
 *   ins = {src: "bin"|"tdv", sessOff (s, Binance), tick, round}
 */
(function (root, factory) {
  const P = factory(root.OF || require("../web/engine.js"), root.OFJ || require("./journal.js"));
  if (typeof module !== "undefined" && module.exports) module.exports = P;
  root.OFP = P;
})(typeof globalThis !== "undefined" ? globalThis : this, function (OF, OFJ) {
  "use strict";

  // day: risk sized by the 1-hour ATR; swing: by the 4-hour ATR with wider targets
  // stops and zones scale with the daily ATR so quiet hours (weekends, Asia) don't shrink them
  // confirmation, lock and timeout rules live in journal.js (OFJ.RULES) so the table and the runner share them
  const PLAN = { day: { ...OFJ.RULES.styles.day, label: "เดย์เทรด", slK: 0.25, clK: 0.10, floor: 0.002, minR: [1.5, 2.5], rounds: 4 },
                 swing: { ...OFJ.RULES.styles.swing, label: "สวิง", slK: 0.5, clK: 0.15, floor: 0.004, minR: [2, 3.5], rounds: 8 } };

  // ---- per-bar flow (exact when the bar carries taker-buy volume) -------------------------
  const exact = b => isFinite(b.bv);
  function split(b) { if (exact(b)) return [b.bv, Math.max(0, b.v - b.bv)]; const r = b.h - b.l, f = r > 0 ? (b.c - b.l) / r : 0.5; return [b.v * f, b.v * (1 - f)]; }
  const delta = b => { const [x, y] = split(b); return x - y; };

  // ---- sessions -----------------------------------------------------------------------
  function sessions(bs, ins) {
    const out = []; let cur = [], key = null;
    for (let i = 0; i < bs.length; i++) {
      const b = bs[i], k = ins.src === "bin" ? Math.floor((b.t - ins.sessOff) / 86400) : null;
      const brk = i > 0 && (ins.src === "bin" ? k !== key : b.t - bs[i - 1].t >= 2400);
      if (brk && cur.length) { out.push(cur); cur = []; }
      cur.push(b); key = k;
    }
    if (cur.length) out.push(cur);
    return out;
  }
  function derive(sess, ins) {
    if (sess.length < 5) return null;
    const ss = sessions(sess, ins), cur = ss[ss.length - 1], prev = ss.length > 1 ? ss[ss.length - 2] : null;
    const prof = OF.profileFromBars(cur, ins.tick), rng = OF.sessionRange(prof, 0.70), mag = OF.valueArea(prof, 0.30);
    let pr = null;
    if (prev && prev.length > 10) { const pp = OF.profileFromBars(prev, ins.tick), r = OF.sessionRange(pp, 0.70), m = OF.valueArea(pp, 0.30);
      pr = { yhi: Math.max(...prev.map(b => b.h)), ylo: Math.min(...prev.map(b => b.l)), poc: r.poc, vah: r.va_high, val: r.va_low, mhi: m[1], mlo: m[0] }; }
    return { cur, prev, prof, rng, mag, pr, last: cur[cur.length - 1] };
  }

  function atrDaily(bars) {
    const d = bars.length > 4 ? bars.slice(0, -1) : bars; if (d.length < 3) return null; // drop the forming day
    const tr = d.slice(1).map((x, i) => Math.max(x.h - x.l, Math.abs(x.h - d[i].c), Math.abs(x.l - d[i].c))).slice(-14);
    return tr.reduce((a, v) => a + v, 0) / tr.length;
  }
  function atrOf(bars, sec) {
    const g = new Map();
    for (const b of bars) { const k = Math.floor(b.t / sec), x = g.get(k); if (!x) g.set(k, { h: b.h, l: b.l, c: b.c }); else { x.h = Math.max(x.h, b.h); x.l = Math.min(x.l, b.l); x.c = b.c; } }
    let hs = [...g.entries()].sort((a, z) => a[0] - z[0]).map(e => e[1]); if (hs.length > 4) hs = hs.slice(0, -1); // drop the forming bar
    if (hs.length < 3) return null;
    const tr = hs.slice(1).map((x, i) => Math.max(x.h - x.l, Math.abs(x.h - hs[i].c), Math.abs(x.l - hs[i].c))).slice(-14);
    return tr.reduce((a, v) => a + v, 0) / tr.length;
  }

  /**
   * ctx = {ins, D (derive), daily (1D bars, forming day last), sess (5m/1m bars), last,
   *        walls: [{p, name}] persistent order-book walls, big: [{p, name}] big-trade zones,
   *        press: buy share of the last 5 minutes in % (null when unknown), oi: {sign, label} (null when unknown)}
   */
  function planLevels(ctx, last, st) {
    const L = [], add = (p, name, w) => { if (p != null && isFinite(p)) L.push({ p, name, w }); }, { D, ins } = ctx;
    if (D.pr) { add(D.pr.yhi, "Y-Hi", 1.5); add(D.pr.vah, "Top zone", 1.5); add(D.pr.mhi, "Magnet บน", 1); add(D.pr.poc, "POC ก่อน", 1.5); add(D.pr.mlo, "Magnet ล่าง", 1); add(D.pr.val, "Bottom zone", 1.5); add(D.pr.ylo, "Y-Lo", 1.5); }
    const dd = ctx.daily.length > 1 ? ctx.daily.slice(0, -1) : [];
    for (const [n, w] of [[5, 1.5], [20, 1.25]]) if (dd.length >= n) { const z = dd.slice(-n); add(Math.max(...z.map(b => b.h)), `สูงสุด ${n} วัน`, w); add(Math.min(...z.map(b => b.l)), `ต่ำสุด ${n} วัน`, w); }
    const vw = OF.vwapBands(D.cur, [2, 3]), v = vw[vw.length - 1];
    if (v) { add(v.vwap, "VWAP", 1); add(v.bands[2][1], "Assist +2σ", 1); add(v.bands[3][1], "Assist +3σ", 1); add(v.bands[2][0], "Assist −2σ", 1); add(v.bands[3][0], "Assist −3σ", 1); }
    add(D.rng.poc, "POC วันนี้", 1);
    for (const w of ctx.walls || []) add(w.p, w.name, 2.5);
    for (const z of ctx.big || []) add(z.p, z.name, 1.5);
    const r0 = Math.floor(last / ins.round) * ins.round; for (let k = -st.rounds + 1; k <= st.rounds; k++) add(r0 + k * ins.round, "เลขกลม", 0.75);
    return { L, v };
  }
  function cluster(levels, side, last, win) {
    // candidate zones on one side of price, scored by the weight of levels stacked within win
    const c = levels.filter(l => side === "res" ? l.p > last + 0.5 * win : l.p < last - 0.5 * win).sort((a, z) => Math.abs(a.p - last) - Math.abs(z.p - last));
    let best = null;
    for (const seed of c.slice(0, 6)) {
      const mem = c.filter(l => Math.abs(l.p - seed.p) <= win), score = mem.reduce((s, l) => s + l.w, 0) - Math.abs(seed.p - last) / win * 0.15;
      if (!best || score > best.score) best = { score, mem, lo: Math.min(...mem.map(l => l.p)), hi: Math.max(...mem.map(l => l.p)) };
    }
    return best;
  }
  function biasModel(ctx, last, v) {
    const cvd = ctx.D.cur.reduce((a, b) => a + delta(b), 0); let score = 0; const why = [];
    if (v) { if (last > v.vwap) { score++; why.push("ราคาเหนือ VWAP"); } else { score--; why.push("ราคาใต้ VWAP"); } }
    if (cvd > 0) { score++; why.push("CVD วันนี้เป็นบวก"); } else if (cvd < 0) { score--; why.push("CVD วันนี้เป็นลบ"); }
    const pr = ctx.press;
    if (pr != null) { if (pr >= 55) { score++; why.push(`แรงซื้อ 5 นาที ${pr.toFixed(0)}%`); } else if (pr <= 45) { score--; why.push(`แรงขาย 5 นาที ${(100 - pr).toFixed(0)}%`); } }
    const o = ctx.oi; if (o && o.sign) { score += o.sign; why.push(`OI 1 ชม.: ${o.label}`); }
    return { score, why, bias: score >= 2 ? "เอียงซื้อ" : score <= -2 ? "เอียงขาย" : "สองทาง รอราคาเข้าโซน" };
  }

  function barIndexAt(bars, tsSec) { let lo = 0, hi = bars.length - 1; if (hi < 0 || tsSec < bars[0].t) return -1;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (bars[m].t <= tsSec) lo = m; else hi = m - 1; } return lo; }
  /** price change vs open-interest change over the same window; oi = [{ts (ms), v}] oldest first, sess = the bars that price it */
  function oiRead(oi, sess, hours) {
    if (oi.length < 2) return null;
    const lastO = oi[oi.length - 1], ref = [...oi].reverse().find(o => o.ts <= lastO.ts - hours * 3600000); if (!ref) return null;
    const px = ts => { const i = barIndexAt(sess, ts / 1000); return i >= 0 ? sess[i].c : null; }, p1 = px(lastO.ts), p0 = px(ref.ts);
    const dOi = (lastO.v - ref.v) / ref.v * 100, dPx = p0 && p1 ? (p1 - p0) / p0 * 100 : null;
    let label = "ทรงตัว", sign = 0;
    if (dPx != null && Math.abs(dOi) >= 0.3) {
      if (dPx > 0.05) [label, sign] = dOi > 0 ? ["Long ใหม่เข้า", 1] : ["Short ปิดหนี", 0];
      else if (dPx < -0.05) [label, sign] = dOi > 0 ? ["Short ใหม่เข้า", -1] : ["Long ปิดหนี", 0];
      else label = dOi > 0 ? "OI เพิ่ม ราคานิ่ง" : "OI ลด ราคานิ่ง";
    }
    return { dOi, dPx, label, sign };
  }

  // ---- plan model (shared by the table, the journal and the back-test) -------------------------------
  function planModel(ctx, styleKey) {
    const { ins, D, last } = ctx, st = PLAN[styleKey];
    if (!D || last == null) return { err: "รอข้อมูล" };
    const dAtr = atrDaily(ctx.daily), hAtr = atrOf(ctx.sess, 3600), atr = dAtr || (hAtr ? hAtr * 4 : null);
    if (!atr) return { err: "ข้อมูลย้อนหลังยังไม่พอคำนวณ ATR" };
    const slGap = Math.max(st.slK * atr, last * st.floor), win = st.clK * atr, gap2 = Math.max(win, 0.25 * atr), { L, v } = planLevels(ctx, last, st);
    const B = biasModel(ctx, last, v), plans = {};
    for (const [side, z] of [["sell", cluster(L, "res", last, win)], ["buy", cluster(L, "sup", last, win)]]) {
      if (!z) continue; const sell = side === "sell", dir = sell ? -1 : 1, entry = sell ? z.lo : z.hi, stop = sell ? z.hi + slGap : z.lo - slGap, risk = Math.abs(entry - stop);
      const ahead = L.filter(l => (l.p - entry) * dir > 0).sort((a, b) => Math.abs(a.p - entry) - Math.abs(b.p - entry));
      const pick = (minR, after) => ahead.find(l => Math.abs(l.p - entry) >= minR * risk && (after == null || Math.abs(l.p - entry) >= Math.abs(after - entry) + gap2))
        || { p: entry + dir * Math.max(minR * risk, after == null ? 0 : Math.abs(after - entry) + gap2), name: `${minR}R (ไม่มีแนวถัดไป)` };
      const t1 = pick(st.minR[0], null), t2 = pick(st.minR[1], t1.p);
      plans[side] = { inst: ins.key, style: styleKey, side, zoneLo: z.lo, zoneHi: z.hi, entry, stop, risk, tp1: t1.p, tp1Name: t1.name, tp2: t2.p, tp2Name: t2.name,
                      names: [...new Set(z.mem.map(l => l.name))].join(" + "), trend: B.score === 0 ? null : (sell ? B.score < 0 : B.score > 0) };
    }
    return { st, atr, atrName: dAtr ? "ATR รายวัน" : "ATR รายวัน ≈ (ATR 1 ชม. × 4)", slGap, last, ...B, plans };
  }

  return { PLAN, exact, split, delta, sessions, derive, atrDaily, atrOf, barIndexAt, oiRead, planLevels, cluster, biasModel, planModel };
});
