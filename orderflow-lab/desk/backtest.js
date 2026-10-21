/* Back-test of the desk plan (plan.js + journal.js, the same files the page runs) over Binance 5-minute candles.
 *
 *   node desk/backtest.js --sym XAUUSDT --ins xau [--data data/backtest/raw] [--metrics data/backtest/metrics]
 *                         [--from 2025-12-11] [--to 2026-10-18] [--fee 0.001] [--out data/backtest/XAUUSDT.json]
 *
 * Data: the public archive at data.binance.vision (monthly and daily kline zips, futures "metrics" zips for open
 * interest), downloaded into the git-ignored data/backtest/. Nothing from the archive goes into the repo.
 *
 * Replay: one tick per closed 5-minute candle, doing what journalTick() in the page does every 10 seconds:
 * the plan is rebuilt from what was known at that moment (the last 1000 five-minute candles, the UTC daily candles
 * with the forming day last, the open-interest read), a plan locks when the price comes within 1.5 stop distances of
 * its entry, a touch, a confirmation candle (15 min day / 1 h swing) closing back beyond the zone within 4 candles,
 * entry at that close, one open trade per style and side, then journal.js resolves it over the following candles.
 *
 * What the page has and the archive does not: order-book walls (weight 2.5) and big-trade zones (1.5) never enter
 * the levels here, the 5-minute buy share comes from the candle's taker-buy volume instead of the tape, and there is
 * no news calendar (the page then enters too, recording the news as unknown). Two choices are stricter than the page:
 * a touch counts only from the candle after the one that locked the plan, and a candle that reaches the stop before
 * the confirmation cancels it even when the same candle also touched the entry.
 */
"use strict";
const fs = require("fs"), path = require("path"), { execFileSync } = require("child_process");
const OFJ = require("./journal.js"), OFP = require("./plan.js");

const INS = {
  xau: { key: "xau", src: "bin", gold: true, tick: 0.1, round: 25, sessOff: 22 * 3600 },
  paxg: { key: "paxg", src: "bin", gold: true, tick: 0.1, round: 25, sessOff: 22 * 3600 },
  btc: { key: "btc", src: "bin", tick: 10, round: 500, sessOff: 0 },
};
const BAR = 300;

function args(argv) {
  const o = { data: path.join(__dirname, "..", "data", "backtest", "raw"), metrics: null, fee: 0.001, oiSym: null };
  for (let i = 0; i < argv.length; i += 2) o[argv[i].replace(/^--/, "")] = argv[i + 1];
  o.fee = +o.fee;
  return o;
}

/** every 5-minute candle of a symbol from the archive zips, oldest first, timestamps in seconds */
function loadBars(dir, sym) {
  const by = new Map();
  for (const f of fs.readdirSync(dir).filter(f => f.startsWith(sym + "-5m-") && f.endsWith(".zip"))) {
    for (const line of execFileSync("unzip", ["-p", path.join(dir, f)], { maxBuffer: 1 << 28 }).toString().split("\n")) {
      if (!/^\d/.test(line)) continue;
      const r = line.split(","); let ms = +r[0]; if (ms > 1e14) ms = Math.floor(ms / 1000); // spot files from 2025 are in µs
      by.set(ms / 1000, { t: ms / 1000, o: +r[1], h: +r[2], l: +r[3], c: +r[4], v: +r[5], bv: +r[9] });
    }
  }
  return [...by.values()].sort((a, z) => a.t - z.t);
}
/** futures open interest every 5 minutes: [{ts (ms), v}] */
function loadOi(dir, sym) {
  if (!dir) return [];
  const out = [];
  for (const f of fs.readdirSync(dir).filter(f => f.startsWith(sym + "-metrics-")).sort())
    for (const line of execFileSync("unzip", ["-p", path.join(dir, f)]).toString().split("\n").slice(1)) {
      const r = line.split(","); if (r.length < 3) continue;
      const ts = Date.parse(r[0].replace(" ", "T") + "Z"), v = +r[2]; if (isFinite(ts) && v > 0) out.push({ ts, v });
    }
  return out.sort((a, z) => a.ts - z.ts);
}

function run(o) {
  const ins = INS[o.ins]; if (!ins) throw new Error("--ins xau | paxg | btc");
  const all = loadBars(o.data, o.sym), oiAll = loadOi(o.metrics, o.oiSym || o.sym);
  const from = o.from ? Date.parse(o.from) / 1000 : -Infinity, to = o.to ? Date.parse(o.to) / 1000 : Infinity;
  const bars = all.filter(b => b.t < to), { trades, funnel } = replay(bars, oiAll, ins, from);
  return { ins: ins.key, sym: o.sym, first: bars.length > 1000 ? bars[Math.max(1000, bars.findIndex(b => b.t >= from))].t : null, last: bars.length ? bars[bars.length - 1].t : null,
           hasOi: oiAll.length > 0, fee: o.fee, trades, funnel };
}

/** the replay itself over 5-minute bars (oldest first) and open interest [{ts, v}]; trades from `from` (unix s) on */
function replay(bars, oiAll, ins, from = -Infinity) {
  const msBars = bars.map(b => ({ start: b.t * 1000, end: (b.t + BAR) * 1000, o: b.o, h: b.h, l: b.l, c: b.c }));
  const endMs = msBars.length ? msBars[msBars.length - 1].end : 0, R = OFJ.RULES;
  const trades = [], funnel = {}, count = k => { funnel[k] = (funnel[k] || 0) + 1; };
  const armed = {}, busyUntil = {};                       // per style:side
  const closedDays = []; let day = null, oiK = 0;

  for (let k = 0; k < bars.length; k++) {
    const b = bars[k], T = (b.t + BAR) * 1000;
    // UTC daily candles as Binance serves them: the forming day last
    const dk = Math.floor(b.t / 86400) * 86400;
    if (!day || day.t !== dk) { if (day) closedDays.push(day); day = { t: dk, o: b.o, h: b.h, l: b.l, c: b.c }; }
    else { day.h = Math.max(day.h, b.h); day.l = Math.min(day.l, b.l); day.c = b.c; }
    while (oiK < oiAll.length && oiAll[oiK].ts <= T) oiK++;
    if (b.t < from || k < 1000) continue;                  // the page starts with 1000 candles of history

    const sess = bars.slice(k - 999, k + 1), last = b.c, hi = b.h, lo = b.l;
    const ctx = { ins, D: OFP.derive(sess, ins), daily: closedDays.slice(-29).concat([day]), sess, last,
      press: b.v > 0 && isFinite(b.bv) ? b.bv / b.v * 100 : 50, oi: oiK ? OFP.oiRead(oiAll.slice(Math.max(0, oiK - 289), oiK), sess, 1) : null, walls: [], big: [] };
    for (const style of Object.keys(OFP.PLAN)) {
      let M = null;
      const st = OFP.PLAN[style];
      for (const side of ["buy", "sell"]) {
        const key = style + ":" + side, sell = side === "sell";
        if (busyUntil[key] != null && T <= busyUntil[key]) { armed[key] = null; continue; }
        if (!M) M = OFP.planModel(ctx, style);
        if (M.err) continue;
        let a = armed[key];
        if (a && !a.touchedAt && (Math.abs(last - a.entry) > R.armRadius * M.slGap || T - a.armedAt > st.armMs)) { count("lock_expired"); a = null; }
        if (!a && M.plans[side] && Math.abs(last - M.plans[side].entry) <= R.armRadius * M.slGap) { a = { ...M.plans[side], armedAt: T, touchedAt: null, bias: M.bias, score: M.score }; count("locked"); armed[key] = a; continue; }
        armed[key] = a || null; if (!a) continue;
        if (!a.touchedAt && (sell ? hi >= a.entry : lo <= a.entry)) { a.touchedAt = b.t * 1000 + 1; count("touched"); }
        if (!a.touchedAt) continue;
        if (sell ? hi >= a.stop : lo <= a.stop) { armed[key] = null; count("stop_before_confirm"); continue; }
        const ok = OFJ.confirmEntry(a, OFJ.candlesOf(sess, st.confSec, T));
        if (!ok.done) continue;
        armed[key] = null;
        if (!ok.ok) { count(ok.reason); continue; }
        count("entered");
        const t = { inst: ins.key, style, side, created: ok.at, entry: ok.entry, stop: a.stop, risk: ok.risk, tp1: a.tp1, tp2: a.tp2, tp1Name: a.tp1Name, tp2Name: a.tp2Name,
                    zoneLo: a.zoneLo, zoneHi: a.zoneHi, names: a.names, trend: a.trend, bias: a.bias, score: a.score, status: "open", tp1Hit: false, tp1At: null };
        const i0 = msBars.findIndex(x => x.start >= t.created), lim = t.created + st.timeoutMs;
        const res = OFJ.resolveTrade(t, msBars.slice(i0, i0 + Math.ceil(st.timeoutMs / (BAR * 1000)) + 2), { now: endMs, timeoutMs: st.timeoutMs });
        Object.assign(t, res.patch); delete t.sim;
        if (!res.final) t.status = "open";
        busyUntil[key] = t.status === "open" ? Infinity : Math.max(t.closedAt || lim, t.created);
        trades.push(t);
      }
    }
  }
  return { trades, funnel };
}

// ---- statistics: closed trades whose outcome the candles decide (journal.js "determinate"), gross and after fees ----
function summarize(trades, fee) {
  const fin = trades.filter(t => OFJ.DETERMINATE.has(t.status) && t.r != null).sort((a, z) => a.closedAt - z.closedAt);
  const net = t => t.r - fee * t.entry / t.risk;          // fee = round-trip cost as a share of the notional
  const s = f => {
    let cum = 0, peak = 0, dd = 0; const rs = fin.map(f);
    for (const r of rs) { cum += r; peak = Math.max(peak, cum); dd = Math.min(dd, cum - peak); }
    const win = rs.filter(r => r > 0), loss = rs.filter(r => r < 0), gw = win.reduce((a, r) => a + r, 0), gl = -loss.reduce((a, r) => a + r, 0);
    return { sum: cum, avg: rs.length ? cum / rs.length : null, winRate: rs.length ? win.length / rs.length : null, pf: gl > 0 ? gw / gl : null, maxDD: dd };
  };
  const by = (k, f) => { const g = {}; for (const t of fin) { const x = f(t); (g[x] = g[x] || []).push(t); } return Object.fromEntries(Object.entries(g).map(([x, a]) => [x, summarize(a, fee).core])); };
  const core = { n: fin.length, tp1Rate: fin.length ? fin.filter(t => t.tp1Hit).length / fin.length : null, gross: s(t => t.r), net: s(net),
                 status: Object.fromEntries([...OFJ.DETERMINATE].map(x => [x, fin.filter(t => t.status === x).length])),
                 costR: fin.length ? fin.reduce((a, t) => a + fee * t.entry / t.risk, 0) / fin.length : null };
  return { core, fin,
    get bySide() { return by("side", t => t.side); },
    get byTrend() { return by("trend", t => t.trend == null ? "ไม่มีทิศ" : t.trend ? "ตามแรง" : "สวนแรง"); },
    get byMonth() { return by("month", t => new Date(t.created).toISOString().slice(0, 7)); } };
}

if (require.main === module) {
  const o = args(process.argv.slice(2)), t0 = Date.now(), res = run(o);
  const pct = x => x == null ? "–" : (x * 100).toFixed(0) + "%", r2 = x => x == null ? "–" : (x >= 0 ? "+" : "") + x.toFixed(2);
  const out = { ...res, styles: {} };
  console.log(`${o.sym} ${new Date(res.first * 1000).toISOString().slice(0, 10)} → ${new Date(res.last * 1000).toISOString().slice(0, 10)} · OI ${res.hasOi ? "yes" : "no"} · fee ${(o.fee * 100).toFixed(2)}% round trip · ${((Date.now() - t0) / 1000).toFixed(0)} s`);
  console.log("funnel", JSON.stringify(res.funnel));
  for (const style of Object.keys(OFP.PLAN)) {
    const S = summarize(res.trades.filter(t => t.style === style), o.fee), c = S.core;
    out.styles[style] = { ...c, bySide: S.bySide, byTrend: S.byTrend, byMonth: S.byMonth,
      open: res.trades.filter(t => t.style === style && t.status === "open").length,
      ambiguous: res.trades.filter(t => t.style === style && t.status === "ambiguous").length };
    console.log(`${style}: ${c.n} trades · TP1 ${pct(c.tp1Rate)} · win ${pct(c.gross.winRate)} · gross ${r2(c.gross.sum)}R (avg ${r2(c.gross.avg)}, PF ${c.gross.pf == null ? "–" : c.gross.pf.toFixed(2)}, maxDD ${r2(c.gross.maxDD)}R)`
      + ` · net ${r2(c.net.sum)}R (avg ${r2(c.net.avg)}, PF ${c.net.pf == null ? "–" : c.net.pf.toFixed(2)}, cost ${r2(c.costR)}R/trade) · ${JSON.stringify(c.status)} · ambiguous ${out.styles[style].ambiguous}`);
    for (const [k, v] of Object.entries({ ...S.bySide, ...S.byTrend })) console.log(`   ${k}: ${v.n} · win ${pct(v.gross.winRate)} · gross ${r2(v.gross.sum)}R · net ${r2(v.net.sum)}R`);
  }
  if (o.out) { fs.mkdirSync(path.dirname(o.out), { recursive: true }); fs.writeFileSync(o.out, JSON.stringify(out)); }
}

module.exports = { run, replay, summarize, loadBars, loadOi, INS };
