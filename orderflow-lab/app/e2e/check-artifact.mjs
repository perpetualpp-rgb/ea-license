// End-to-end check of the claude.ai artifact build (dist/flow-lab-artifact.html, from `npm run build:single`).
// The page runs with a stubbed window.claude.use("mcp") whose Binance connector answers in Binance's
// REST shapes with synthetic numbers (no network, no recorded market data). Checks that all four screens
// say the data is real Binance data seen privately through the owner's connector, that the fallbacks to
// demo explain themselves in Thai, and that the stub only ever received allowlisted read-only tools.
// Run: NODE_PATH=$(npm root -g) node e2e/check-artifact.mjs
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const here = path.dirname(fileURLToPath(import.meta.url));
const file = path.join(here, "..", "dist", "flow-lab-artifact.html");
const out = process.env.OUT || path.join(here, "..", "..", "data", "app-e2e-artifact");
fs.mkdirSync(out, { recursive: true });
const ORIGIN = "https://artifact.test";
// claude.ai wraps the artifact content in its own document; do the same here
const page0 = `<!doctype html><html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"></head><body>${fs.readFileSync(file, "utf8")}</body></html>`;

const ALLOW = ["futures_usds.klineCandlestickData", "futures_usds.orderBook", "futures_usds.compressedAggregateTradesList",
  "futures_usds.symbolPriceTicker", "spot.klines", "spot.depth", "spot.aggTrades",
  "futures_usds.openInterestStatistics", "futures_usds.markPrice", "futures_usds.topTraderLongShortRatioPositions", "futures_usds.takerBuySellVolume"];
const OWNER = "ข้อมูลจริงจาก Binance ผ่าน connector ของคุณ · ดูส่วนตัว ห้ามเผยแพร่/ขาย";

const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok, detail }); console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? " — " + detail : ""}`); };

/** runs inside the page before the app: window.claude with a Binance connector that answers synthetic data */
function installStub(mode) {
  window.__binCalls = [];
  if (mode === "none") return;
  const MIN = 60000, IV = { "1m": MIN, "5m": 5 * MIN, "15m": 15 * MIN, "30m": 30 * MIN, "1h": 60 * MIN };
  const BASE = { BTCUSDT: 60000, XAUUSDT: 2400 };
  const s = (n, d = 2) => n.toFixed(d);
  const px = (base, ms) => base + Math.round(Math.sin(ms / 3600000) * base * 0.002 * 100) / 100;
  const klines = (a, now, base) => {
    const iv = IV[a.interval], limit = Number(a.limit ?? 500), last = Math.floor(now / iv) * iv;
    const first = a.startTime != null ? Math.ceil(a.startTime / iv) * iv : last - (limit - 1) * iv, rows = [];
    for (let t = first; t <= last && rows.length < limit; t += iv) {
      const o = px(base, t), c = px(base, t + iv), v = 10, buy = c >= o ? 6 : 4;
      rows.push([t, s(o), s(Math.max(o, c) + 1), s(Math.min(o, c) - 1), s(c), s(v, 3), t + iv - 1, s(v * c), 100, s(buy, 3), s(buy * c), "0"]);
    }
    return rows;
  };
  const trades = (a, now, base) => {
    const limit = Number(a.limit ?? 500), newest = Math.floor((now - 2000) / 1000), from = a.fromId != null ? Number(a.fromId) : newest - limit + 1, out = [];
    for (let id = from; id <= newest && out.length < limit; id++) out.push({ a: id, p: s(px(base, id * 1000)), q: s(id % 50 === 0 ? 25 : 0.1 + (id % 7) * 0.05, 3), f: id * 3, l: id * 3 + 2, T: id * 1000 + 250, m: id % 2 === 0 });
    return out;
  };
  const depth = (now, base, spot) => {
    const mid = px(base, now), step = base > 10000 ? 10 : 0.5, bids = [], asks = [];
    for (let k = 1; k <= 120; k++) { bids.push([s(mid - k * step), s(!spot && k === 30 ? 40 : 1 + (k % 5) * 0.1, 3)]); asks.push([s(mid + k * step), s(1 + (k % 5) * 0.1, 3)]); }
    return spot ? { lastUpdateId: now, bids, asks } : { lastUpdateId: now, E: now - 5, T: now - 10, bids, asks };
  };
  const answer = (tool, a) => {
    const now = Date.now(), base = BASE[a.symbol] ?? 100;
    switch (tool) {
      case "futures_usds.klineCandlestickData": case "spot.klines": return klines(a, now, base);
      case "futures_usds.compressedAggregateTradesList": case "spot.aggTrades": return trades(a, now, base);
      case "futures_usds.orderBook": return depth(now, base, false);
      case "spot.depth": return depth(now, base, true);
      case "futures_usds.openInterestStatistics": {
        const end = Math.floor(now / (5 * MIN)) * 5 * MIN - 5 * MIN;
        return Array.from({ length: 289 }, (_, i) => ({ symbol: a.symbol, sumOpenInterest: s(i === 288 ? 81200 : 80000, 3), sumOpenInterestValue: "1", timestamp: end - (288 - i) * 5 * MIN }));
      }
      case "futures_usds.markPrice": return { symbol: a.symbol, markPrice: s(px(base, now) + 5, 8), indexPrice: s(px(base, now) + 2, 8), estimatedSettlePrice: "0", lastFundingRate: "0.00010000", interestRate: "0.00010000", nextFundingTime: (Math.floor(now / 28800000) + 1) * 28800000, time: now - 1000 };
      case "futures_usds.symbolPriceTicker": return { symbol: a.symbol, price: s(px(base, now)), time: now - 500 };
      default: throw { code: "tool_error", message: "stub has no " + tool };
    }
  };
  let approved = mode !== "approval";
  const mcp = Object.freeze({
    async callTool(server, tool, input, options) {
      window.__binCalls.push({ server, tool, toolName: input && input.toolName, options: JSON.stringify(options) });
      await new Promise((r) => setTimeout(r, 5));
      if (mode === "not_connected") throw { code: "server_not_connected", server, message: "no connector named Binance" };
      if (!approved) {
        // the shell asks only while a call waits: the automatic first calls are declined, the click's call is allowed
        if (window.__allowNext) approved = true;
        else throw { code: "approval_required", server, message: "declined" };
      }
      const p = answer(input.toolName, input.arguments || {});
      const text = JSON.stringify(p);
      // the connector sometimes wraps its JSON in {result}
      return input.toolName.includes("orderBook") ? { payload: { result: text }, content: [{ type: "text", text }] } : { payload: p, content: [{ type: "text", text }] };
    },
  });
  window.claude = Object.freeze({ use: (name) => new Promise((r) => setTimeout(() => r(name === "mcp" ? mcp : null), 30)) });
}

async function open(browser, viewport, mode) {
  const ctx = await browser.newContext({ viewport, acceptDownloads: true, locale: "th-TH", timezoneId: "Asia/Bangkok" });
  const net = [];
  await ctx.route("**/*", (r) => {
    const u = r.request().url();
    if (u.startsWith(ORIGIN)) return r.fulfill({ body: page0, contentType: "text/html" });
    // fonts are the artifact's own stylesheet; api.example.test is the member API the settings test types in
    if (!/fonts\.(googleapis|gstatic)\.com|api\.example\.test/.test(u)) net.push(u);
    return r.abort();
  });
  await ctx.addInitScript(installStub, mode);
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => { if (m.type() === "error" && !/ERR_FAILED|net::/.test(m.text())) errors.push(m.text()); });
  return { ctx, page, errors, net };
}

async function audit(page, label) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  check(`${label}: no horizontal scroll`, overflow <= 0, `overflow ${overflow}px`);
  const small = await page.evaluate(() => [...document.querySelectorAll("button, a[href], input, select, textarea, [role=tab], [role=radio], [role=switch]")]
    .filter((el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && el.type !== "checkbox"
      && !el.classList.contains("sr-only") && (r.width < 44 || r.height < 40); })
    .map((el) => `${el.tagName.toLowerCase()}.${el.className}:${Math.round(el.getBoundingClientRect().width)}x${Math.round(el.getBoundingClientRect().height)}`));
  check(`${label}: touch targets ≥ 44px wide and ≥ 40px tall`, small.length === 0, small.slice(0, 5).join(", "));
  const claim = await page.evaluate(() => /บันทึกบนเซิร์ฟเวอร์แล้ว|synced|สำรองแล้ว|ถูกต้อง/i.test(document.body.innerText));
  check(`${label}: never claims server save/sync or "ถูกต้อง"`, !claim);
}

const realLabels = async (page, label) => {
  const banner = (await page.locator(".offline.owner").count()) === 1 && (await page.textContent(".offline.owner")).includes(OWNER);
  const demo = await page.locator("text=ข้อมูลตัวอย่าง").count();
  check(`${label}: real-source label shown, no demo label`, banner && demo === 0, `banner ${banner}, demo labels ${demo}`);
};

const browser = await chromium.launch({ executablePath: fs.existsSync("/opt/pw-browsers/chromium") ? "/opt/pw-browsers/chromium" : undefined }).catch(() => chromium.launch());
const allCalls = [];
const allErrors = [];
const allNet = [];
const collect = async (c) => { allCalls.push(...(await c.page.evaluate(() => window.__binCalls))); allErrors.push(...c.errors); allNet.push(...c.net); };

for (const [name, viewport] of [["390", { width: 390, height: 844 }], ["320", { width: 320, height: 640 }], ["1440", { width: 1440, height: 900 }]]) {
  const c = await open(browser, viewport, "ok");
  const { page } = c;
  await page.goto(`${ORIGIN}/#/overview`);
  await page.waitForSelector(".hero .price");
  const hero = await page.textContent(".hero");
  check(`${name} overview: price with live badge and the connector tag`, /\d{2},\d{3}\.\d{2} USDT/.test(hero) && /ข้อมูลสด/.test(hero) && /connector ของคุณ/.test(hero), hero.replace(/\s+/g, " ").slice(0, 160));
  await page.waitForSelector(".obs");
  check(`${name} overview: observations are measured facts`, /CVD [+−][\d.]+ BTC ใน 5 นาที/.test(await page.textContent(".section")));
  await realLabels(page, `${name} overview`);
  await audit(page, `${name} overview`);
  await page.screenshot({ path: path.join(out, `${name}-overview.png`), fullPage: true });

  await page.goto(`${ORIGIN}/#/analyze?id=binance-um-btcusdt&panel=walls`);
  await page.waitForSelector(".chart-box canvas");
  await page.waitForSelector("#panel-walls table.data tbody tr");
  const walls = await page.textContent("#panel-walls");
  check(`${name} analyze: walls from the book, tracked by this page`, /BID \(รอซื้อ\)/.test(walls) && /ใหม่|ยืน|เติม|ลดลง/.test(walls) && /อายุนับจากที่หน้านี้เห็นครั้งแรก/.test(walls), walls.replace(/\s+/g, " ").slice(0, 160));
  check(`${name} analyze: big trades state their percentile threshold`, /เปอร์เซ็นไทล์ที่ 99/.test(walls));
  await page.click('summary:has-text("OI และ Funding")');
  const oi = await page.textContent("#panel-oi");
  check(`${name} analyze: OI in base asset and estimated funding`, /81\.2K BTC/.test(oi) && /ประมาณการรอบถัดไป/.test(oi) && /\+0\.0100%/.test(oi), oi.replace(/\s+/g, " ").slice(0, 160));
  const prices = await page.textContent(".price-types");
  await page.waitForFunction(() => Number(document.querySelector(".chart-box")?.dataset.oi) > 0, null, { timeout: 8000 }).catch(() => undefined);
  const ds = await page.evaluate(() => ({ ...document.querySelector(".chart-box").dataset }));
  check(`${name} analyze: walls and OI drawn on the chart from the connector data`, +ds.walls > 0 && +ds.oi > 0, JSON.stringify(ds));
  check(`${name} analyze: last, mark and index typed separately`, /ซื้อขายล่าสุด/.test(prices) && /Mark/.test(prices) && /Index/.test(prices) && !/ไม่มีในตลาดนี้/.test(prices));
  await realLabels(page, `${name} analyze`);
  await audit(page, `${name} analyze`);
  await page.screenshot({ path: path.join(out, `${name}-analyze.png`), fullPage: true });

  await page.goto(`${ORIGIN}/#/alerts`);
  await page.waitForSelector("h1:has-text('แจ้งเตือน')");
  await realLabels(page, `${name} alerts`);
  if (name === "390") {
    await page.click('button:has-text("สร้างการแจ้งเตือน")');
    await page.fill('[role=dialog] input[type=number]', "5");
    await page.click('[role=dialog] button[type=submit]');
    await page.waitForSelector("text=ค่าล่าสุด");
    check("390 alerts: a rule is evaluated from the connector data", /ค่าล่าสุด 1\.50 %/.test(await page.textContent(".stack")));
  }
  await audit(page, `${name} alerts`);
  await page.screenshot({ path: path.join(out, `${name}-alerts.png`), fullPage: true });

  await page.goto(`${ORIGIN}/#/journal`);
  await page.waitForSelector("h1:has-text('บันทึกของฉัน')");
  await realLabels(page, `${name} journal`);
  await audit(page, `${name} journal`);
  await page.screenshot({ path: path.join(out, `${name}-journal.png`), fullPage: true });

  if (name === "390") {
    // settings: connector choice only in this build; API test token kept in sessionStorage only
    await page.click('button[aria-label="ตั้งค่าและบัญชี"]');
    const opts = await page.locator("[role=dialog] select option").allTextContents();
    check("settings offers the owner's connector", opts.includes("Binance ผ่าน connector ของคุณ (ดูส่วนตัว)"), opts.join(" | "));
    await page.selectOption("[role=dialog] select", "api");
    await page.fill('[role=dialog] input[type=url]', "https://api.example.test");
    await page.fill('[role=dialog] input[type=password]', "test-token-123");
    await page.click('[role=dialog] button[type=submit]');
    await page.waitForSelector(".offline.api");
    const st = await page.evaluate(() => ({ s: sessionStorage.getItem("flowlab.apiToken"), l: JSON.stringify(localStorage), u: location.href }));
    check("API test token is in sessionStorage only, never localStorage or the URL", st.s === "test-token-123" && !st.l.includes("test-token-123") && !st.u.includes("test-token"), JSON.stringify(st).slice(0, 160));
    check("member API mode is labelled differently from the owner preview", (await page.locator(".offline.owner").count()) === 0 && /เซิร์ฟเวอร์ Flow Lab/.test(await page.textContent(".offline.api")));
  }
  await collect(c);
  await c.ctx.close();
}

// fallbacks: demo data with the desk's Thai explanation
{
  const c = await open(browser, { width: 390, height: 844 }, "not_connected");
  await c.page.goto(`${ORIGIN}/#/overview`);
  await c.page.waitForSelector(".source-issue");
  const t = await c.page.textContent(".offline.demo");
  check("connector missing → demo data with the reason in Thai", /ข้อมูลตัวอย่าง/.test(t) && /ยังไม่ได้เชื่อม connector Binance เพิ่มได้ที่ claude\.ai → Settings → Connectors/.test(t), t.slice(0, 200));
  await c.page.waitForSelector(".hero .price");
  check("connector missing → screens still work on labelled demo data", (await c.page.locator(".offline.owner").count()) === 0);
  await audit(c.page, "390 fallback");
  await c.page.screenshot({ path: path.join(out, "390-fallback.png"), fullPage: true });
  await collect(c);
  await c.ctx.close();
}
{
  const c = await open(browser, { width: 390, height: 844 }, "approval");
  await c.page.goto(`${ORIGIN}/#/overview`);
  await c.page.waitForSelector('.source-issue button:has-text("อนุญาตและดึงข้อมูล")');
  check("approval declined → explains and offers the approve button", /ต้องอนุญาตให้หน้านี้ใช้ Binance ก่อน/.test(await c.page.textContent(".source-issue")));
  await c.page.evaluate(() => { window.__allowNext = true; });
  await c.page.click('.source-issue button');
  await c.page.waitForSelector(".offline.owner");
  await c.page.waitForSelector(".hero .price");
  check("after approving, the real-source label replaces demo", (await c.page.locator(".offline.demo").count()) === 0 && /ข้อมูลสด/.test(await c.page.textContent(".hero")));
  await collect(c);
  await c.ctx.close();
}
{
  const c = await open(browser, { width: 390, height: 844 }, "none");
  await c.page.goto(`${ORIGIN}/#/overview`);
  await c.page.waitForSelector(".source-issue");
  check("outside claude.ai → demo with 'open in claude.ai' and no retry button", /เปิดหน้านี้ใน claude\.ai/.test(await c.page.textContent(".source-issue")) && (await c.page.locator(".source-issue button").count()) === 0);
  await collect(c);
  await c.ctx.close();
}

await browser.close();
const names = [...new Set(allCalls.map((x) => x.toolName))].sort();
check("stub received only allowlisted tool names", allCalls.length > 0 && allCalls.every((x) => ALLOW.includes(x.toolName)), names.join(", "));
check("every call went to Binance tool_execute with cache: false", allCalls.every((x) => x.server === "Binance" && x.tool === "tool_execute" && x.options === '{"cache":false}'), `${allCalls.length} calls`);
check("the page made no network requests of its own", allNet.length === 0, allNet.slice(0, 3).join(" "));
check("no page errors", allErrors.length === 0, allErrors.slice(0, 5).join(" | "));
const failed = results.filter((r) => !r.ok).length;
console.log(`\n${results.length - failed} passed, ${failed} failed · ${allCalls.length} connector calls (${names.length} tools) · screenshots in ${out}`);
process.exit(failed ? 1 : 0);
