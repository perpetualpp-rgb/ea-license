const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");
// stubbed render check: node desk/check.js (needs playwright); screenshots go to data/ (git-ignored)
const OUT = path.join(__dirname, "..", "data");
const RULE_VERSION = require("./journal.js").RULES.version;
const OLD_RECORDS = 1003; // older than the live window: loaded page by page (1000 per query), two share the page-edge time
(async () => {
  const html = fs.readFileSync(path.join(__dirname, "orderflow-desk.html"), "utf8");
  // stub of window.claude.use("mcp"): Binance tool_execute returns real payload shapes, tdv returns column bars + quotes
  const stub = `(() => {
    let s = 11; const r = () => { s = (s * 16807) % 2147483647; return s / 2147483647; };
    const now = Date.now(), IV = { "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400 };
    const base = sym => sym === "XAUUSDT" ? 4167.5 : 81900, stepOf = sym => sym === "XAUUSDT" ? 1.2 : 60;
    const kc = {}, klines = (sym, iv, n, end) => kc[sym + iv + n + (end || "")] || (kc[sym + iv + n + (end || "")] = klinesGen(sym, iv, n, end));
    const klinesGen = (sym, iv, n, end) => { const sec = IV[iv], st = stepOf(sym) * Math.sqrt(sec / 300); let p = base(sym) - st * 3; const out = []; const t0 = Math.floor((end ? end + 1 : now) / 1000 / sec) * sec - (end ? n : n - 1) * sec;
      for (let i = 0; i < n; i++) { const o = p, c = o + (r() - 0.5) * st * 2, h = Math.max(o, c) + r() * st * 0.6, l = Math.min(o, c) - r() * st * 0.6, v = (sym === "XAUUSDT" ? 40 : 15) * (0.3 + r() * 2 + (r() < 0.04 ? 6 : 0)), bv = v * (0.25 + r() * 0.5);
        out.push([(t0 + i * sec) * 1000, o, h, l, c, v.toFixed(3), (t0 + i * sec + sec) * 1000 - 1, "0", 100, bv.toFixed(3), "0", "0"]); p = c; }
      const d = base(sym) - out[out.length - 1][4]; for (const k of out) for (const j of [1, 2, 3, 4]) k[j] = (k[j] + d).toFixed(2);
      return out; };
    let id = { XAUUSDT: 93677000, BTCUSDT: 4100000000 };
    const depth = sym => { const mid = base(sym), tk = sym === "XAUUSDT" ? 0.01 : 0.01, bids = [], asks = []; let pb = mid - tk, pa = mid;
      for (let i = 0; i < 1000; i++) { const gap = sym === "XAUUSDT" ? tk * (1 + Math.floor(r() * 2)) : tk * (1 + Math.floor(r() * 40)); pb -= gap; pa += gap;
        const w = q => (r() < 0.006 ? q * 40 : q); bids.push([pb.toFixed(2), w(0.02 + r() * 0.5).toFixed(3)]); asks.push([pa.toFixed(2), w(0.02 + r() * 0.5).toFixed(3)]); }
      bids.unshift([(mid - tk).toFixed(2), "35.7"]); asks.unshift([mid.toFixed(2), "48.8"]); return { lastUpdateId: 1, E: Date.now(), T: Date.now(), bids, asks }; };
    const agg = (sym, a) => { const out = [], n = 150; let start = a.fromId != null ? a.fromId : id[sym] - 999; const end = Math.min(start + (a.limit || 500), id[sym] + n);
      for (let k = start; k < end; k++) { const big = r() < 0.03; out.push({ a: k, p: (base(sym) + (r() - 0.5) * stepOf(sym) * 4).toFixed(2), q: (big ? (sym === "XAUUSDT" ? 5 + r() * 40 : 0.5 + r() * 4) : r() * 0.3).toFixed(3), f: k, l: k, T: Date.now() - (end - k) * 40, m: r() < 0.5 }); }
      id[sym] = Math.max(id[sym], end); return out; };
    const bin = (tool, a) => { if (/klines|klineCandlestick/i.test(tool)) return klines(a.symbol, a.interval, a.limit, a.endTime); if (/depth|orderBook/.test(tool)) return depth(a.symbol);
      if (/ggTrades|AggregateTrades/.test(tool)) return agg(a.symbol, a);
      if (tool === "futures_usds.openInterestStatistics") { const per = { "5m": 3e5, "15m": 9e5, "30m": 18e5, "1h": 36e5, "4h": 144e5, "1d": 864e5 }[a.period] || 3e5, end = Math.floor((a.endTime || now) / per) * per;
        return Array.from({ length: a.limit }, (_, i) => ({ symbol: a.symbol, sumOpenInterest: String(131000 + i * 3 + r() * 50), sumOpenInterestValue: String(5.4e8 + i * 1e4), timestamp: end - (a.limit - 1 - i) * per })); }
      if (tool === "futures_usds.markPrice") return { symbol: a.symbol, markPrice: "4167.9", lastFundingRate: "0.00010000", nextFundingTime: now + 3 * 3600000, time: now };
      if (tool === "futures_usds.topTraderLongShortRatioPositions") return Array.from({ length: a.limit }, (_, i) => { const l = 0.7 + i * 0.003; return { symbol: a.symbol, longAccount: String(l), shortAccount: String(1 - l), longShortRatio: String(l / (1 - l)), timestamp: now - (a.limit - i) * 3600000 }; });
      if (tool === "futures_usds.takerBuySellVolume") return Array.from({ length: a.limit }, (_, i) => ({ buyVol: String(400 + r() * 100), sellVol: String(400 + r() * 100), buySellRatio: "1.0", timestamp: now - (a.limit - i) * 3600000 })); if (/PriceTicker/.test(tool)) return { price: "4167.56", symbol: "XAUUSDT", time: Date.now() }; throw { code: "tool_error", message: "unknown " + tool }; };
    const gc = () => { const c = { t: [], o: [], h: [], l: [], c: [], v: [] }; let p = 4186; for (let i = 0; i < 300; i++) { const o = p, cl = o + (r() - 0.5) * 6; c.t.push(Math.floor(now / 1000) - (window.__gcAgoH || 0) * 3600 - (300 - i) * 300); c.o.push(o); c.h.push(Math.max(o, cl) + 1); c.l.push(Math.min(o, cl) - 1); c.c.push(cl); c.v.push(Math.round(50 + r() * 400)); p = cl; } return c; };
    window.__calls = []; window.__dbWrites = []; window.__dbReads = [];
    // in-memory db (where / orderBy / limit / get / onSnapshot like the real store, pages capped at 1000)
    // with one seeded open trade so the resolver has work to do, and older closed records for pagination
    const store = new Map(), subs = [];
    const deliver = () => { for (const f of subs) f(); };
    const OPS = { "<": (a, b) => a < b, "<=": (a, b) => a <= b, ">": (a, b) => a > b, ">=": (a, b) => a >= b, "==": (a, b) => a === b };
    const colRef = (path, filters = [], order = null, lim = null) => {
      const docsOf = () => { let d = [...store].filter(([k]) => k.startsWith(path + "/") && !k.slice(path.length + 1).includes("/")).map(([k, v]) => ({ id: k.slice(path.length + 1), exists: true, data: () => v }));
        for (const [f, op, v] of filters) d = d.filter(x => OPS[op](x.data()[f], v));
        if (order) d.sort((a, z) => (order[1] === "desc" ? -1 : 1) * (a.data()[order[0]] - z.data()[order[0]]) || (a.id < z.id ? -1 : 1));
        return lim ? d.slice(0, lim) : d; };
      const snap = () => { const docs = docsOf(); return { docs, size: docs.length, empty: !docs.length, docChanges: () => [], metadata: { fromCache: false, hasPendingWrites: false } }; };
      return { path,
        doc: id => ({ id, path: path + "/" + id, set: async d => { window.__dbWrites.push(["set", id, d.status]); store.set(path + "/" + id, d); setTimeout(deliver, 0); },
          update: async d => { window.__dbWrites.push(["update", id, d.status, d.tp1Hit]); store.set(path + "/" + id, { ...store.get(path + "/" + id), ...d }); setTimeout(deliver, 0); },
          delete: async () => { store.delete(path + "/" + id); setTimeout(deliver, 0); } }),
        where: (f, op, v) => colRef(path, [...filters, [f, op, v]], order, lim), orderBy: (f, dir = "asc") => colRef(path, filters, [f, dir], lim), limit: n => colRef(path, filters, order, n),
        get: async () => { if (lim > 1000) throw { code: "invalid_argument", message: "page size over 1000" }; const r = snap(); window.__dbReads.push(r.size); return r; },
        onSnapshot: next => { if (lim > 1000) throw { code: "invalid_argument" }; subs.push(() => next(snap())); setTimeout(deliver, 0); return () => {}; } }; };
    const db = { doc: path => ({ collection: c => colRef(path + "/" + c) }), collection: colRef };
    { const bars = klines("XAUUSDT", "5m", 1000), k = bars.length - 30, entry = +bars[k][4], after = bars.slice(k + 1), H = Math.max(...after.map(b => +b[2])), L = Math.min(...after.map(b => +b[3]));
      store.set("data/users/u1/journal/trades/seed", { kind: "simulation", rule_version: "${RULE_VERSION}", inst: "xau", style: "day", side: "buy", created: bars[k][0] + 300000, entry, stop: L - 0.5, risk: entry - (L - 0.5), tp1: entry + (H - entry) * 0.5, tp2: H + 3,
        tp1Name: "x", tp2Name: "y", zoneLo: entry - 1, zoneHi: entry, names: "seed", trend: true, bias: "x", conditions: { news: "ok" }, status: "open", tp1Hit: false, tp1At: null, closedAt: null, exit: null, r: null }); }
    for (let i = 0; i < ${OLD_RECORDS}; i++) { const created = now - 10 * 86400000 - Math.min(i, 999) * 60000 - (i > 1000 ? (i - 1000) * 60000 : 0), win = i % 3 !== 0;
      store.set("data/users/u1/journal/trades/old" + i, { kind: "simulation", rule_version: "${RULE_VERSION}", inst: "xau", style: "day", side: "buy", created, entry: 100, stop: 90, risk: 10, tp1: 115, tp2: 130,
        trend: i % 2 === 0, status: win ? "tp2" : "sl", r: win ? 2.25 : -1, tp1Hit: win, tp1At: null, closedAt: created + 3600000, exit: win ? 130 : 90 }); }
    window.claude = { use: async n => n === "db" ? db : n === "user" ? { id: async () => "u1" } : n !== "mcp" ? null : ({
      callTool: async (srv, tool, input) => { window.__calls.push(srv + ":" + tool + ":" + (input.toolName || "") + (input.arguments && input.arguments.endTime ? ":endTime" : "")); if (srv === "Binance" && tool === "tool_execute") return { payload: bin(input.toolName, input.arguments) }; throw { code: "not_in_manifest" }; },
      watchTool(srv, tool, inp, h) { setTimeout(() => { if (tool.includes("calendar")) h({ type: "data", result: { payload: { status: "ok", result: [
          { date: new Date(now + 50 * 60000).toISOString(), title: "Core Inflation Rate MoM", importance: 1, forecast: 0.3, previous: 0.3, actual: null, unit: "%", period: "Sep" },
          { date: new Date(now + 5 * 3600000).toISOString(), title: "Housing Starts", importance: 0, forecast: 1.3, previous: 1.275, actual: null, scale: "M" }] } } });
        else if (tool.includes("ohlcv")) h({ type: "data", result: { payload: { bars: gc() } } });
        else h({ type: "data", result: { payload: { data: inp.symbol.includes("GC") ? { close: 4186.7, change: -0.4, update_mode: "streaming" } : inp.symbol.includes("THB") ? { close: 32.45, change: 0.1 } : { close: 4165.42, change: -0.13 } } } }); }, 50); return () => {}; } }) };
  })();`;
  fs.mkdirSync(OUT, { recursive: true });
  fs.writeFileSync(path.join(OUT, "desk-preview.html"), `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><style>:root{color-scheme:light}body{margin:0;font:14px system-ui}[hidden]{display:none!important}</style><script>${stub}</script></head><body>${html}</body></html>`);
  const b = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium" }).catch(() => chromium.launch()); const errs = [];
  for (const [name, o] of [["light", { viewport: { width: 1280, height: 1000 }, colorScheme: "light" }], ["phone-dark", { viewport: { width: 390, height: 844 }, colorScheme: "dark" }]]) {
    const ctx = await b.newContext(o); const p = await ctx.newPage();
    p.on("pageerror", e => errs.push(name + ": " + e.message)); p.on("console", m => { if (m.type() === "error" && !/ERR_FAILED/.test(m.text())) errs.push(name + ": " + m.text()); });
    await p.route(/fonts\.(googleapis|gstatic)\.com/, r => r.abort());
    if (process.env.LWC_FILE) await p.route(/unpkg\.com\/lightweight-charts/, r => r.fulfill({ path: process.env.LWC_FILE, contentType: "application/javascript" }));
    await p.goto("file://" + path.join(OUT, "desk-preview.html"));
    // advance through several depth/trade polls so walls become persistent
    await p.waitForTimeout(3500);
    const info = async () => p.evaluate(() => ({ scrollW: document.documentElement.scrollWidth, ov: document.getElementById("ovMain").hidden, strip: [...document.querySelectorAll("#strip .q .v")].map(e => e.textContent).join(" | "),
      oi: (document.getElementById("oiHint") || {}).textContent || "", ob: document.querySelectorAll("#obBox tr").length, tape: document.querySelectorAll("#tblTape tr").length, flow: document.getElementById("flowTiles").textContent.slice(0, 160), err: document.getElementById("errTop").textContent, tags: [...document.querySelectorAll(".tag")].map(t => t.textContent).join(","), cot: document.getElementById("cardCot").hidden }));
    console.log(name, "xau", JSON.stringify(await info()));
    if (name === "light") {
      await p.click('#segMode button[data-k="gc"]'); await p.waitForTimeout(300); console.log("mode gc last:", await p.textContent("#strip .q.hl .v"));
      await p.click('#segMode button[data-k="native"]'); await p.waitForTimeout(200);
      // drag the chart right and zoom out until the left edge asks for older candles
      await p.locator("#lwMain").scrollIntoViewIfNeeded(); await p.waitForTimeout(200);
      const box = await p.locator("#lwMain").boundingBox(), cx = box.x + box.width / 2, cy = box.y + box.height * 0.35;
      const legend0 = await p.textContent("#lwLegend");
      await p.mouse.move(cx, cy); for (let k = 0; k < 6; k++) await p.mouse.wheel(0, 400);
      for (let k = 0; k < 3; k++) { await p.mouse.move(box.x + 100, cy); await p.mouse.down(); await p.mouse.move(box.x + box.width - 120, cy, { steps: 10 }); await p.mouse.up(); await p.waitForTimeout(500); }
      await p.mouse.move(box.x + 120, cy); await p.waitForTimeout(200);
      console.log("legend", legend0.slice(0, 60), "->", (await p.textContent("#lwLegend")).slice(0, 60));
      console.log("history calls", await p.evaluate(() => window.__calls.filter(c => c.endsWith(":endTime")).length));
      await p.screenshot({ path: path.join(OUT, "desk-xau.png"), fullPage: true });
      await p.locator('section[aria-label="แผนเทรด"]').screenshot({ path: path.join(OUT, "desk-plan-day.png") });
      await p.click('#segPlan button[data-k="swing"]'); await p.waitForTimeout(200); await p.locator('section[aria-label="แผนเทรด"]').screenshot({ path: path.join(OUT, "desk-plan-swing.png") });
      await p.click('#segPlan button[data-k="day"]'); await p.waitForTimeout(200);
      await p.click('#segInst button[data-k="btc"]'); await p.waitForTimeout(1200); console.log("btc", JSON.stringify(await info())); await p.locator('section[aria-label="แผนเทรด"]').screenshot({ path: path.join(OUT, "desk-plan-btc.png") }); await p.screenshot({ path: path.join(OUT, "desk-btc.png"), fullPage: true });
      // GC trading: real XAUUSDT walls are drawn at GC price (needs two book polls = 10 s+ for persistence)
      await p.click('#segInst button[data-k="gc"]'); await p.waitForTimeout(12500); console.log("gc", JSON.stringify(await info()));
      const gw = await p.evaluate(() => ({ n: +(document.getElementById("lwMain").dataset.xauWalls || 0), hint: document.getElementById("wallHint").textContent }));
      console.log("gc xau walls", gw.n, "|", gw.hint.slice(0, 150));
      if (!(gw.n > 0 && /แปลงเป็นราคา GC ด้วยส่วนต่าง/.test(gw.hint))) errs.push("gc: XAUUSDT walls not drawn on the GC chart");
      await p.screenshot({ path: path.join(OUT, "desk-gc.png"), fullPage: true });
      // GC closed (session ended 40 h ago): no walls (the gap would be stale), the reason is shown; OI still covers the GC session
      await p.evaluate(() => { window.__gcAgoH = 40; }); await p.click('#segInst button[data-k="xau"]'); await p.waitForTimeout(600);
      await p.click('#segInst button[data-k="gc"]'); await p.waitForTimeout(3000);
      const gs = await p.evaluate(() => ({ n: +(document.getElementById("lwMain").dataset.xauWalls || 0), hint: document.getElementById("wallHint").textContent, oi: document.getElementById("oiHint").textContent }));
      console.log("gc closed:", gs.n, "|", gs.hint.slice(0, 120), "|", gs.oi);
      if (gs.n !== 0 || !/ตลาด GC ไม่ได้เปิด/.test(gs.hint) || !/OI XAUUSDT/.test(gs.oi)) errs.push("gc closed: expected no walls with a reason and the OI pane");
      await p.screenshot({ path: path.join(OUT, "desk-gc-closed.png"), fullPage: true });
      await p.evaluate(() => { window.__gcAgoH = 0; });
      await p.click('#segTf button[data-k="1H"]'); await p.waitForTimeout(400);
      await p.click('#segInst button[data-k="xau"]'); await p.waitForTimeout(800);
      const calls = await p.evaluate(() => [...new Set(window.__calls)]); console.log("calls", calls.join(" "));
      const writes = await p.evaluate(() => window.__dbWrites), reads = await p.evaluate(() => window.__dbReads);
      console.log("db writes", JSON.stringify(writes), "| journal pages read", JSON.stringify(reads));
      const track = await p.evaluate(() => document.getElementById("jTrack").textContent);
      const total = +((track.match(/บันทึกทั้งหมด ([\d,]+) ไม้/) || [])[1] || "0").replace(/,/g, "");
      if (total !== OLD_RECORDS + 1) errs.push(`journal: expected ${OLD_RECORDS + 1} records after pagination, page shows ${total}`);
      // the seed: stop below every later low, TP2 above every later high, TP1 at half way -> TP1 then break-even, or ambiguous, or still open
      const seed = writes.filter(w => w[1] === "seed").pop();
      if (seed && !["be", "ambiguous", undefined].includes(seed[2])) errs.push("journal: seed resolved to " + seed[2]);
      console.log("journal records", total, "· seed", JSON.stringify(seed || null));
      await p.locator('section[aria-label="บันทึกผลแผน"]').screenshot({ path: path.join(OUT, "journal.png") });
      console.log("journal", await p.evaluate(() => document.getElementById("jStats").textContent.slice(0, 200)), "|", await p.evaluate(() => document.getElementById("tblJournal").textContent.slice(0, 160)));
      console.log("news banner", await p.evaluate(() => document.getElementById("newsBanner").hidden ? "hidden" : document.getElementById("newsBanner").textContent.slice(0, 120)));
      console.log("fut", await p.evaluate(() => document.getElementById("futTiles").textContent.slice(0, 260)));
    } else await p.screenshot({ path: path.join(OUT, "desk-phone-dark.png"), fullPage: true });
    await ctx.close();
  }
  await b.close(); console.log(errs.length ? "ERRORS\n" + errs.join("\n") : "no page errors");
})();
