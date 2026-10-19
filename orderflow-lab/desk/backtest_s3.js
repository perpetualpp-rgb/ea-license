/* Back-test of strategy 3 of the owner's Gold Plan Desk (±1/2/3 SD around the day's open) with that page's own
 * engine: GoldEngine.strategy3 does the lines and replays the trades exactly as the page does; this file only feeds it.
 *
 *   node desk/backtest_s3.js --engine <Gold Plan Desk engine.js> --sym XAUUSDT [--mode fade|trend]
 *        [--data data/backtest/raw] [--gvz data/backtest/gvz/GVZ_History.csv] [--cost 0.4] [--out data/backtest/s3-XAUUSDT-fade.json]
 *
 * engine.js is a file of the owner's Gold Plan Desk artifact and is not part of this repository.
 * Inputs: Binance 5-minute candles (data.binance.vision) stand in for PEPPERSTONE:XAUUSD; days are Globex days from
 * 17:00 Chicago and the Saturday and Sunday ones (gold closed, Binance open) are dropped; %IV is the CBOE GVZ close
 * of the last date before the day's start (CBOE's own daily history, the same closes TradingView shows). There is no
 * GC1! history here, so the day's diff is unknown and the gold.f round-number entries of fade mode do not happen.
 * The engine runs month by month with 40 days of history before each month; every trade is kept once (by its id).
 * Cost: `--cost` dollars per ounce per round trip (the Gold Plan Desk research used $0.4), shown as R next to gross R.
 */
"use strict";
const fs = require("fs"), path = require("path");
const { loadBars } = require("./backtest.js");

function args(argv) {
  const o = { data: path.join(__dirname, "..", "data", "backtest", "raw"), gvz: path.join(__dirname, "..", "data", "backtest", "gvz", "GVZ_History.csv"), mode: "fade", cost: 0.4 };
  for (let i = 0; i < argv.length; i += 2) o[argv[i].replace(/^--/, "")] = argv[i + 1];
  o.cost = +o.cost;
  return o;
}

/** CBOE GVZ_History.csv (DATE mm/dd/yyyy, GVZ) as daily bars stamped 13:30 UTC, the time TradingView stamps them */
function loadGvz(file) {
  return fs.readFileSync(file, "utf8").trim().split("\n").slice(1).map(l => {
    const [d, v] = l.split(","), [m, day, y] = d.split("/").map(Number);
    return { t: Date.UTC(y, m - 1, day, 13, 30) / 1000, o: +v, h: +v, l: +v, c: +v, v: 0 };
  }).filter(b => isFinite(b.c) && b.c > 0).sort((a, z) => a.t - z.t);
}

function run(E, bars5, gvz, o) {
  const weekday = b => { const d = new Date(E.tradingDayKey(b.t) + "T00:00:00Z").getUTCDay(); return d >= 1 && d <= 5; };
  const bars = bars5.filter(weekday).map(b => ({ t: b.t, o: b.o, h: b.h, l: b.l, c: b.c, v: b.v }));
  const byId = new Map(), days = new Map();
  const first = new Date(bars[0].t * 1000), last = new Date(bars[bars.length - 1].t * 1000);
  for (let y = first.getUTCFullYear(), m = first.getUTCMonth(); y < last.getUTCFullYear() || (y === last.getUTCFullYear() && m <= last.getUTCMonth()); m === 11 ? (y++, m = 0) : m++) {
    const m0 = Date.UTC(y, m, 1) / 1000, m1 = Date.UTC(y, m + 1, 1) / 1000, k0 = new Date(m0 * 1000).toISOString().slice(0, 10), k1 = new Date(m1 * 1000).toISOString().slice(0, 10);
    const chunk = bars.filter(b => b.t >= m0 - 40 * 86400 && b.t < m1 + 2 * 86400);
    if (chunk.length < 500) continue;
    const m15 = E.aggregate(chunk, 900), src = { m15, fine: chunk, xau1d: E.aggregateDays(chunk), gvz1d: gvz, gc1d: null, xau5m: null, gc5m: null, basis: null };
    const s = E.strategy3(src, null, { mode: o.mode });
    if (!s.ok) continue;
    for (const d of s.days) if (d.key >= k0 && d.key < k1 && !d.partial && d.sd1) days.set(d.key, { key: d.key, iv: d.iv, ivFallback: d.ivFallback, sd1: d.sd1, base: d.base, trend: d.trend });
    for (const t of s.trades) if (t.day >= k0 && t.day < k1 && !byId.has(t.id)) byId.set(t.id, t);
  }
  return { trades: [...byId.values()].sort((a, z) => a.t - z.t), days: [...days.values()].sort((a, z) => (a.key < z.key ? -1 : 1)) };
}

function stats(trades, cost) {
  const done = trades.filter(t => t.pts != null && t.reliable);
  const risk = t => Math.abs(t.entry - t.sl), net = t => (t.pts - cost) / risk(t);
  const s = f => { const v = done.map(f), w = v.filter(x => x > 0), l = v.filter(x => x < 0), gw = w.reduce((a, x) => a + x, 0), gl = -l.reduce((a, x) => a + x, 0);
    let cum = 0, peak = 0, dd = 0; for (const x of v) { cum += x; peak = Math.max(peak, cum); dd = Math.min(dd, cum - peak); }
    return { sum: cum, avg: v.length ? cum / v.length : null, winRate: v.length ? w.length / v.length : null, pf: gl > 0 ? gw / gl : null, maxDD: dd }; };
  const outcomes = {}; for (const t of done) outcomes[t.outcome] = (outcomes[t.outcome] || 0) + 1;
  return { n: done.length, gross: s(t => t.r), net: s(net), outcomes, avgRisk: done.length ? done.reduce((a, t) => a + risk(t), 0) / done.length : null };
}

if (require.main === module) {
  const o = args(process.argv.slice(2));
  if (!o.engine || !o.sym) throw new Error("--engine <Gold Plan Desk engine.js> --sym XAUUSDT|PAXGUSDT");
  const E = require(path.resolve(o.engine)), t0 = Date.now();
  const res = run(E, loadBars(o.data, o.sym), loadGvz(o.gvz), o);
  const pct = x => x == null ? "–" : (x * 100).toFixed(0) + "%", r1 = x => x == null ? "–" : (x >= 0 ? "+" : "") + x.toFixed(1), pf = x => x == null ? "–" : x.toFixed(2);
  const all = stats(res.trades, o.cost);
  console.log(`${o.sym} strategy 3 ${o.mode} · ${res.days.length} days ${res.days[0].key} → ${res.days[res.days.length - 1].key} · cost $${o.cost}/oz · ${((Date.now() - t0) / 1000).toFixed(0)} s`);
  const line = (name, s) => console.log(`${name.padEnd(16)} ${String(s.n).padStart(5)} · win ${pct(s.gross.winRate).padStart(4)} · gross ${r1(s.gross.sum).padStart(7)}R PF ${pf(s.gross.pf)} · net ${r1(s.net.sum).padStart(7)}R PF ${pf(s.net.pf)} win ${pct(s.net.winRate)} · maxDD ${r1(s.net.maxDD)}R`);
  line("all", all);
  for (const kind of [...new Set(res.trades.map(t => t.kind))]) for (const n of [1, 2, 3]) {
    const g = res.trades.filter(t => t.kind === kind && Math.abs(t.n) === n); if (g.length) line(`${kind} ±${n}SD`, stats(g, o.cost));
  }
  for (const y of [...new Set(res.trades.map(t => t.day.slice(0, 4)))]) line(`year ${y}`, stats(res.trades.filter(t => t.day.startsWith(y)), o.cost));
  console.log("outcomes", JSON.stringify(all.outcomes), "· avg SL $" + (all.avgRisk || 0).toFixed(2));
  if (o.out) { fs.mkdirSync(path.dirname(o.out), { recursive: true }); fs.writeFileSync(o.out, JSON.stringify({ sym: o.sym, mode: o.mode, cost: o.cost, days: res.days, trades: res.trades })); }
}

module.exports = { run, stats, loadGvz };
