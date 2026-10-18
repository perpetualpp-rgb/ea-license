/* Order Flow Lab desk journal — the pure rules behind "บันทึกผลแผน".
 *
 * Personal research only: every record is a SIMULATION from candles (OHLC), gross
 * of fees, spread, slippage and funding, with no real fills. desk/build.py inlines
 * this file into the page (window.OFJ); tests/test_journal.py runs it under node.
 *
 *   bar    = {start, end, o, h, l, c}            times in ms, end = start + bar length
 *   record = {side:"buy"|"sell", entry, stop, tp1, tp2, created, style, rule_version, ...}
 *
 * Management: half the position closes at TP1 and the stop of the other half moves
 * to the entry; that half runs to TP2, the stop, or the style's timeout.
 */
(function (root, factory) {
  const J = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = J;
  root.OFJ = J;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const RULES = {
    version: "2026-10-18.2",
    tp1Share: 0.5,            // share closed at TP1
    minTp1R: 1,               // TP1 must sit at least this many R on the profit side of the fill
    maxConfirm: 4,            // confirmation candles allowed after the touch
    armRadius: 1.5,           // a plan locks when price is within this many SL distances of its entry
    newsPadMs: 30 * 60000,    // no new entries within this distance of a high-impact US release
    styles: {
      day: { confSec: 900, conf: "15 นาที", armMs: 4 * 3600000, arm: "4 ชม.", timeoutMs: 24 * 3600000, timeout: "24 ชม." },
      swing: { confSec: 3600, conf: "1 ชม.", armMs: 24 * 3600000, arm: "24 ชม.", timeoutMs: 72 * 3600000, timeout: "72 ชม." },
    },
  };
  const DETERMINATE = new Set(["sl", "be", "tp2", "timeout"]);
  const CHECKPOINT_MS = 6 * 3600000;  // save walk progress at least this often, so a short bar history never loses it
  const STATUS = { open: "เปิดอยู่", sl: "โดน SL", be: "ถึง TP1 แล้วออกที่ทุน", tp2: "ถึง TP2", timeout: "หมดเวลา",
                   ambiguous: "ไม่ชัด (แท่งเดียวแตะทั้งสองฝั่ง)", nodata: "ข้อมูลไม่พอตรวจ", invalid: "แผนผิดรูป" };

  // ---- text shown in the plan table: generated from RULES, so it says what the runner checks ------
  function confirmText(style, side, f) {
    const st = RULES.styles[style], sell = side === "sell";
    return `ราคา${sell ? "ขึ้น" : "ลง"}มาแตะ ${f.entry} แล้วแท่ง ${st.conf} ปิดกลับ${sell ? "ลงใต้ " + f.zoneLo : "ขึ้นเหนือ " + f.zoneHi} ภายใน ${RULES.maxConfirm} แท่ง`
      + ` · เข้าที่ราคาปิดแท่งนั้น เมื่อ TP1 อยู่ฝั่งกำไรห่าง ≥ ${RULES.minTp1R}R จากราคาเข้า`
      + ` · ไม่เข้าภายใน ±${RULES.newsPadMs / 60000} นาทีของข่าวแรง US (ปฏิทินโหลดไม่ได้ = บันทึกว่า "ไม่ทราบข่าว")`
      + ` · ถึง TP1 ปิดครึ่ง เลื่อน SL ส่วนที่เหลือมาที่ทุน ไป TP2 · ปิดเมื่อครบ ${st.timeout}`;
  }
  function cancelText(style, side, f) {
    const st = RULES.styles[style];
    return `ราคาแตะ ${f.stop} ก่อนแท่งยืนยัน · ไม่มีแท่งยืนยันใน ${RULES.maxConfirm} แท่ง · ล็อกแล้วราคาไม่แตะ ${f.entry} ใน ${st.arm} หรือห่างออกไปเกิน ${RULES.armRadius} เท่าของระยะ SL`;
  }

  // ---- news state at a moment: never read a calendar that did not load as "no news" -------------
  function newsStatus(cal, cover, ts, padMs = RULES.newsPadMs) {
    if (!cover || !cover.loaded || !(ts - padMs >= cover.from && ts + padMs <= cover.to)) return { status: "unavailable" };
    const e = (cal || []).find(x => x.importance >= 1 && Math.abs(ts - x.ts) <= padMs);
    return e ? { status: "news", title: e.title, at: e.ts } : { status: "ok" };
  }

  // ---- confirmation candles from closed bars ---------------------------------------------------------
  // bars: [{t (unix s), h, l, c}]; asOf = when the bars were fetched. A candle that had ended by then
  // holds only bars that had closed, so its close is final; later candles are left out.
  function candlesOf(bars, sec, asOf) {
    const m = new Map();
    for (const b of bars) {
      const k = Math.floor(b.t / sec) * sec, c = m.get(k);
      if (!c) m.set(k, { start: k * 1000, end: (k + sec) * 1000, h: b.h, l: b.l, c: b.c, last: b.t });
      else { c.h = Math.max(c.h, b.h); c.l = Math.min(c.l, b.l); if (b.t > c.last) { c.c = b.c; c.last = b.t; } }
    }
    return [...m.values()].filter(c => c.end <= asOf).sort((a, z) => a.start - z.start);
  }

  // ---- entry: first candle after the touch that closes back beyond the zone, with signed checks ------
  function confirmEntry(a, candles) {
    const sell = a.side === "sell", dir = sell ? -1 : 1;
    const after = candles.filter(c => c.end > a.touchedAt).slice(0, RULES.maxConfirm);
    const ok = after.find(c => sell ? c.c < a.zoneLo : c.c > a.zoneHi);
    if (!ok) return after.length >= RULES.maxConfirm ? { done: true, ok: false, reason: "no_confirm" } : { done: false };
    const entry = ok.c, risk = (entry - a.stop) * dir, r1 = (a.tp1 - entry) * dir / risk, r2 = (a.tp2 - entry) * dir / risk;
    const no = reason => ({ done: true, ok: false, reason, entry, candle: ok });
    if (!(risk > 0)) return no("stop_wrong_side");
    if (!(r1 > 0)) return no("tp1_wrong_side");
    if (r1 < RULES.minTp1R) return no("tp1_under_min_r");
    if (!(r2 > r1)) return no("tp2_not_beyond_tp1");
    return { done: true, ok: true, entry, risk, r1, r2, at: ok.end, candle: { start: ok.start, end: ok.end, c: ok.c } };
  }

  // ---- resolution over closed bars ------------------------------------------------------------------
  // Two walks run side by side. When a bar's OHLC cannot tell which of two levels came first, the
  // "worst" walk takes the unfavourable order and the "best" walk the favourable one; any such bar
  // makes the record ambiguous, reported with both R values and kept out of headline statistics.
  function freshWalk(t) { return { phase: t.tp1Hit ? 1 : 0, tp1At: t.tp1At || null, done: null, amb: false }; }
  function initSim(t) {
    return { at: t.tp1Hit && t.tp1At ? t.tp1At : t.created, lastC: null, cons: freshWalk(t), opt: freshWalk(t) };
  }
  function clone(x) { return JSON.parse(JSON.stringify(x)); }

  // prices are mirrored for sells (p -> -p) so one long-side routine covers both
  function stepBar(w, b, L, best) {
    if (w.done) return;
    const R = p => (p - L.E) / L.risk, k = RULES.tp1Share;
    const fin = (status, p, r) => { w.done = { status, exit: p * L.dir, r, at: b.end }; };
    const choose = () => { w.amb = true; return best; };
    const rBe = p => k * R(L.T1) + (1 - k) * R(p), rTp2 = rBe(L.T2);
    const be = (p = L.E) => fin("be", p, rBe(p)), tp2 = () => fin("tp2", L.T2, rTp2);
    // the rest of a bar that is wholly after TP1 (h and l both come later)
    const runner = () => {
      if (b.o != null && b.o <= L.E) return be(b.o);           // opened through the moved stop
      if (b.o != null && b.o >= L.T2) return tp2();
      const hitE = b.l <= L.E, hitT2 = b.h >= L.T2;
      if (hitE && hitT2) return choose() ? tp2() : be();
      if (hitE) return be();
      if (hitT2) return tp2();
    };
    if (w.phase === 1) return runner();
    if (b.o != null && b.o <= L.S) return fin("sl", b.o, R(b.o));   // gap through the stop: out at the open
    const tp1 = () => { w.phase = 1; w.tp1At = b.end; };
    if (b.o != null && b.o >= L.T1) { tp1(); return runner(); }      // gap through TP1: filled at TP1
    const hitS = b.l <= L.S, hitT1 = b.h >= L.T1;
    if (!hitT1) { if (hitS) fin("sl", L.S, -1); return; }
    if (hitS) {                                                      // stop and TP1 in one bar
      if (!choose()) return fin("sl", L.S, -1);
      tp1();
      return b.h >= L.T2 ? tp2() : be();                             // TP1 first, then down through entry (l <= stop)
    }
    tp1();                                                           // TP1 crossed inside the bar: only the high is surely later
    const beMaybe = b.l <= L.E, beSure = b.c <= L.E;
    if (b.h >= L.T2) return beMaybe ? (choose() ? tp2() : be()) : tp2();
    if (beSure) return be();
    if (beMaybe && !choose()) return be();                           // the low may have come before TP1
  }

  function material(sim) { return JSON.stringify([sim.cons.phase, sim.cons.done && sim.cons.done.status, sim.cons.amb, sim.opt.phase, sim.opt.done && sim.opt.done.status, sim.opt.amb]); }

  /**
   * Advance one record over bars and say what changed.
   *   bars: [{start, end, o, h, l, c}] sorted; o: {now (ms, bars ending later are ignored), timeoutMs}
   * Returns {patch, final, material}: write the patch when `material` is true; `final` means closed.
   */
  function resolveTrade(t, bars, o) {
    const dir = t.side === "sell" ? -1 : 1, risk = (t.entry - t.stop) * dir;
    if (!(risk > 0) || !((t.tp1 - t.entry) * dir > 0) || !((t.tp2 - t.tp1) * dir > 0))
      return { final: true, material: true, patch: { status: "invalid", r: null, exit: null, closedAt: o.now } };
    const L = { dir, risk, E: dir * t.entry, S: dir * t.stop, T1: dir * t.tp1, T2: dir * t.tp2 };
    const lim = t.created + o.timeoutMs, sim = t.sim ? clone(t.sim) : initSim(t), before = material(sim);
    let pastLimit = sim.at >= lim, nodata = false;
    const first = bars.find(b => b.end <= o.now);
    if (first && first.start - sim.at >= first.end - first.start) nodata = true;   // history no longer reaches back
    for (const b of nodata ? [] : bars) {
      if (b.start < sim.at) continue;
      if (b.end > o.now) break;                                      // never decide on a candle that has not closed
      if (b.start >= lim) { pastLimit = true; break; }
      const lb = dir > 0 ? b : { start: b.start, end: b.end, o: b.o == null ? null : -b.o, h: -b.l, l: -b.h, c: -b.c };
      stepBar(sim.cons, lb, L, false);
      stepBar(sim.opt, lb, L, true);
      sim.at = b.end; sim.lastC = b.c;
      if (sim.at >= lim) pastLimit = true;
      if (sim.cons.done && sim.opt.done) break;
    }
    if (pastLimit) {
      for (const w of [sim.cons, sim.opt]) {
        if (w.done) continue;
        if (sim.lastC == null) { nodata = true; break; }
        const R = p => (p - t.entry) * dir / risk, r = w.phase === 1 ? RULES.tp1Share * R(t.tp1) + (1 - RULES.tp1Share) * R(sim.lastC) : R(sim.lastC);
        w.done = { status: "timeout", exit: sim.lastC, r, at: lim };
      }
    }
    const c = sim.cons, p = sim.opt, amb = c.amb || p.amb;
    const patch = { sim, tp1Hit: c.phase === 1, tp1At: c.phase === 1 ? c.tp1At : null };
    let final = false;
    if (nodata && !(c.done && p.done)) { Object.assign(patch, { status: "nodata", r: null, exit: null, closedAt: o.now }); final = true; }
    else if (c.done && p.done) {
      final = true;
      Object.assign(patch, amb
        ? { status: "ambiguous", r: null, rCons: c.done.r, rOpt: p.done.r, exit: null, cons: c.done, opt: p.done, closedAt: Math.max(c.done.at, p.done.at) }
        : { status: c.done.status, r: c.done.r, exit: c.done.exit, closedAt: c.done.at });
    }
    const progressed = sim.at - (t.sim ? t.sim.at : t.created) >= CHECKPOINT_MS;
    return { patch, final, material: final || progressed || material(sim) !== before };
  }

  // ---- statistics over records of one rule version ------------------------------------------------------
  function stats(recs, version = RULES.version) {
    const cur = recs.filter(t => t.rule_version === version), fin = cur.filter(t => DETERMINATE.has(t.status) && t.r != null);
    const amb = cur.filter(t => t.status === "ambiguous"), sum = fin.reduce((a, t) => a + t.r, 0), tp1 = fin.filter(t => t.tp1Hit).length;
    const add = (arr, k) => arr.reduce((a, t) => a + (t[k] || 0), 0);
    return { version, n: fin.length, tp1, tp1Rate: fin.length ? tp1 / fin.length : null, sum, avg: fin.length ? sum / fin.length : null,
             open: cur.filter(t => t.status === "open").length, ambiguous: amb.length, ambCons: add(amb, "rCons"), ambOpt: add(amb, "rOpt"),
             nodata: cur.filter(t => t.status === "nodata" || t.status === "invalid").length,
             newsUnavailable: fin.filter(t => t.conditions && t.conditions.news === "unavailable").length,
             otherVersions: recs.length - cur.length };
  }

  return { RULES, STATUS, DETERMINATE, confirmText, cancelText, newsStatus, candlesOf, confirmEntry, stepBar, resolveTrade, stats };
});
