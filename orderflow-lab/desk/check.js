const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");
// stubbed render check: node desk/check.js (needs playwright); screenshots go to data/ (git-ignored)
const OUT = path.join(__dirname, "..", "data");
(async () => {
  const html = fs.readFileSync(path.join(__dirname, "orderflow-desk.html"), "utf8");
  // stub of window.claude.use("mcp"): Binance tool_execute returns real payload shapes, tdv returns column bars + quotes
  const stub = `(() => {
    let s = 11; const r = () => { s = (s * 16807) % 2147483647; return s / 2147483647; };
    const now = Date.now(), IV = { "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400 };
    const base = sym => sym === "XAUUSDT" ? 4167.5 : 81900, stepOf = sym => sym === "XAUUSDT" ? 1.2 : 60;
    const klines = (sym, iv, n) => { const sec = IV[iv], st = stepOf(sym) * Math.sqrt(sec / 300); let p = base(sym) - st * 3; const out = []; const t0 = Math.floor(now / 1000 / sec) * sec - (n - 1) * sec;
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
    const bin = (tool, a) => { if (/klines|klineCandlestick/i.test(tool)) return klines(a.symbol, a.interval, a.limit); if (/depth|orderBook/.test(tool)) return depth(a.symbol);
      if (/ggTrades|AggregateTrades/.test(tool)) return agg(a.symbol, a); if (/PriceTicker/.test(tool)) return { price: "4167.56", symbol: "XAUUSDT", time: Date.now() }; throw { code: "tool_error", message: "unknown " + tool }; };
    const gc = () => { const c = { t: [], o: [], h: [], l: [], c: [], v: [] }; let p = 4186; for (let i = 0; i < 300; i++) { const o = p, cl = o + (r() - 0.5) * 6; c.t.push(Math.floor(now / 1000) - (300 - i) * 300); c.o.push(o); c.h.push(Math.max(o, cl) + 1); c.l.push(Math.min(o, cl) - 1); c.c.push(cl); c.v.push(Math.round(50 + r() * 400)); p = cl; } return c; };
    window.__calls = [];
    window.claude = { use: async n => n !== "mcp" ? null : ({
      callTool: async (srv, tool, input) => { window.__calls.push(srv + ":" + tool + ":" + (input.toolName || "")); if (srv === "Binance" && tool === "tool_execute") return { payload: bin(input.toolName, input.arguments) }; throw { code: "not_in_manifest" }; },
      watchTool(srv, tool, inp, h) { setTimeout(() => { if (tool.includes("ohlcv")) h({ type: "data", result: { payload: { bars: gc() } } });
        else h({ type: "data", result: { payload: { data: inp.symbol.includes("GC") ? { close: 4186.7, change: -0.4, update_mode: "streaming" } : inp.symbol.includes("THB") ? { close: 32.45, change: 0.1 } : { close: 4165.42, change: -0.13 } } } }); }, 50); return () => {}; } }) };
  })();`;
  fs.mkdirSync(OUT, { recursive: true });
  fs.writeFileSync(path.join(OUT, "desk-preview.html"), `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><style>:root{color-scheme:light}body{margin:0;font:14px system-ui}[hidden]{display:none!important}</style><script>${stub}</script></head><body>${html}</body></html>`);
  const b = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium" }).catch(() => chromium.launch()); const errs = [];
  for (const [name, o] of [["light", { viewport: { width: 1280, height: 1000 }, colorScheme: "light" }], ["phone-dark", { viewport: { width: 390, height: 844 }, colorScheme: "dark" }]]) {
    const ctx = await b.newContext(o); const p = await ctx.newPage();
    p.on("pageerror", e => errs.push(name + ": " + e.message)); p.on("console", m => { if (m.type() === "error" && !/ERR_FAILED/.test(m.text())) errs.push(name + ": " + m.text()); });
    await p.route(/fonts\.(googleapis|gstatic)\.com/, r => r.abort());
    await p.goto("file://" + path.join(OUT, "desk-preview.html"));
    // advance through several depth/trade polls so walls become persistent
    await p.waitForTimeout(1500);
    await p.evaluate(() => { /* nothing */ });
    const info = async () => p.evaluate(() => ({ scrollW: document.documentElement.scrollWidth, ov: document.getElementById("ovMain").hidden, strip: [...document.querySelectorAll("#strip .q .v")].map(e => e.textContent).join(" | "),
      ob: document.querySelectorAll("#obBox tr").length, tape: document.querySelectorAll("#tblTape tr").length, flow: document.getElementById("flowTiles").textContent.slice(0, 160), err: document.getElementById("errTop").textContent, tags: [...document.querySelectorAll(".tag")].map(t => t.textContent).join(","), cot: document.getElementById("cardCot").hidden }));
    console.log(name, "xau", JSON.stringify(await info()));
    if (name === "light") {
      await p.click('#segMode button[data-k="gc"]'); await p.waitForTimeout(300); console.log("mode gc last:", await p.textContent("#strip .q.hl .v"));
      await p.click('#segMode button[data-k="native"]'); await p.waitForTimeout(200);
      await p.screenshot({ path: path.join(OUT, "desk-xau.png"), fullPage: true });
      await p.locator('section[aria-label="แผนเทรด"]').screenshot({ path: path.join(OUT, "desk-plan-day.png") });
      await p.click('#segPlan button[data-k="swing"]'); await p.waitForTimeout(200); await p.locator('section[aria-label="แผนเทรด"]').screenshot({ path: path.join(OUT, "desk-plan-swing.png") });
      await p.click('#segPlan button[data-k="day"]'); await p.waitForTimeout(200);
      await p.click('#segInst button[data-k="btc"]'); await p.waitForTimeout(1200); console.log("btc", JSON.stringify(await info())); await p.locator('section[aria-label="แผนเทรด"]').screenshot({ path: path.join(OUT, "desk-plan-btc.png") }); await p.screenshot({ path: path.join(OUT, "desk-btc.png"), fullPage: true });
      await p.click('#segInst button[data-k="gc"]'); await p.waitForTimeout(800); console.log("gc", JSON.stringify(await info())); await p.screenshot({ path: path.join(OUT, "desk-gc.png"), fullPage: true });
      await p.click('#segTf button[data-k="1H"]'); await p.waitForTimeout(400);
      await p.click('#segInst button[data-k="xau"]'); await p.waitForTimeout(800);
      const calls = await p.evaluate(() => [...new Set(window.__calls)]); console.log("calls", calls.join(" "));
    } else await p.screenshot({ path: path.join(OUT, "desk-phone-dark.png"), fullPage: true });
    await ctx.close();
  }
  await b.close(); console.log(errs.length ? "ERRORS\n" + errs.join("\n") : "no page errors");
})();
