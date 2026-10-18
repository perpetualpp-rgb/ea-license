// End-to-end check of the production build against a RUNNING Flow Lab server: a throwaway
// PostgreSQL loaded by ingest from collector files, the real API (server/devstack.py), and the
// app in headless Chromium switched to the API through its own settings screen.
// Every number the screens show is compared with what the API answered for the same request.
//
//   npm run build
//   DATA=<collector data_dir> COLLECTOR_CONFIG=<its config> NODE_PATH=$(npm root -g) node e2e/check-api.mjs
import { createRequire } from "node:module";
import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const here = path.dirname(fileURLToPath(import.meta.url));
const dist = path.join(here, "..", "dist");
const server = path.join(here, "..", "..", "server");
const out = process.env.OUT || path.join(here, "..", "..", "data", "app-e2e-api");
fs.mkdirSync(out, { recursive: true });
const DATA = process.env.DATA;
if (!DATA) throw new Error("set DATA to a collector data_dir");
const API_PORT = Number(process.env.API_PORT || 8093), APP_PORT = Number(process.env.APP_PORT || 5175);
const APP = `http://localhost:${APP_PORT}`; // localhost is a secure context, like https in production
const TYPES = { ".html": "text/html", ".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".webmanifest": "application/manifest+json" };

const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok }); console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? " — " + detail : ""}`); };
const fmt = (v, d) => v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });

// -- 1. the server: postgres + migrations + ingest of the collector files + API -------------
const stackFile = path.join(os.tmpdir(), `flowlab-stack-${process.pid}.json`);
const args = ["devstack.py", "--data", DATA, "--port", String(API_PORT), "--cors", APP, "--out", stackFile];
if (process.env.COLLECTOR_CONFIG) args.push("--collector-config", process.env.COLLECTOR_CONFIG);
const stack = spawn("python3", args, { cwd: server, stdio: ["ignore", "pipe", "pipe"] });
let stackLog = "";
stack.stdout.on("data", (b) => { stackLog += b; });
stack.stderr.on("data", (b) => { stackLog += b; });
const stop = () => { try { stack.kill("SIGTERM"); } catch { /* gone */ } };
process.on("exit", stop);
for (let i = 0; i < 600 && !fs.existsSync(stackFile); i++) {
  if (stack.exitCode !== null) { console.log(stackLog); throw new Error("devstack exited"); }
  await new Promise((r) => setTimeout(r, 100));
}
const { api, token, delayed_token: delayedToken } = JSON.parse(fs.readFileSync(stackFile, "utf8"));
fs.rmSync(stackFile);
console.log(stackLog.split("\n").find((l) => l.startsWith("ingest:")) ?? "");
const get = async (p, t = token) => { const r = await fetch(api + p, { headers: { Authorization: `Bearer ${t}` } }); if (!r.ok) throw new Error(`${r.status} ${p}`); return r.json(); };

// -- 2. the app: production build served on localhost ----------------------------------------
const web = http.createServer((req, res) => {
  const u = new URL(req.url, APP);
  const file = path.join(dist, u.pathname === "/" ? "index.html" : u.pathname);
  if (!file.startsWith(dist) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); return res.end("not found"); }
  res.writeHead(200, { "Content-Type": TYPES[path.extname(file)] || "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
});
await new Promise((r) => web.listen(APP_PORT, "localhost", r));

const browser = await chromium.launch({ executablePath: fs.existsSync("/opt/pw-browsers/chromium") ? "/opt/pw-browsers/chromium" : undefined }).catch(() => chromium.launch());
const errors = [];
const apiCalls = new Set(), otherHosts = new Set();

async function audit(page, label) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  check(`${label}: no horizontal scroll`, overflow <= 0, `overflow ${overflow}px`);
  const claim = await page.evaluate(() => /บันทึกบนเซิร์ฟเวอร์แล้ว|synced|สำรองแล้ว|ถูกต้อง/i.test(document.body.innerText));
  check(`${label}: no unbacked claims (server saved / synced / ถูกต้อง)`, !claim);
}

async function connect(page, tok) {
  await page.goto(`${APP}/#/overview`);
  await page.waitForSelector(".hero .price");
  const btn = page.locator('button[aria-label="ตั้งค่าและบัญชี"]:visible, .nav-btn:has-text("ตั้งค่า"):visible').first();
  await btn.click();
  await page.selectOption('[role=dialog] select', "api");
  await page.fill('[role=dialog] input[type=url]', api);
  await page.fill('[role=dialog] input[type=password]', tok);
  await page.click('[role=dialog] button[type=submit]');
  await page.waitForSelector("text=ข้อมูลจากเซิร์ฟเวอร์ Flow Lab");
}

for (const [name, viewport] of [["390", { width: 390, height: 844 }], ["1440", { width: 1440, height: 900 }]]) {
  const ctx = await browser.newContext({ viewport, locale: "th-TH", timezoneId: "Asia/Bangkok" });
  await ctx.route(/fonts\.(googleapis|gstatic)\.com/, (r) => r.abort());
  const page = await ctx.newPage();
  page.on("pageerror", (e) => errors.push(`${name}: ${e.message}`));
  page.on("console", (m) => { if (m.type() === "error" && !/ERR_FAILED|net::/.test(m.text())) errors.push(`${name}: ${m.text()}`); });
  page.on("request", (r) => {
    const u = new URL(r.url());
    if (u.origin === api) apiCalls.add(u.pathname.replace(/\/\d+(\/|$)/, "/{id}$1"));
    else if (u.origin !== APP && !/fonts\./.test(u.host)) otherHosts.add(u.host);
  });
  await connect(page, token);

  // overview: instruments, last price and freshness straight from the API
  const list = await get("/v1/instruments");
  const ins = list.items[0];
  const q = await get(`/v1/instruments/${ins.instrument_id}/quote`);
  const dec = ins.tick_size ? (ins.tick_size.replace(/0+$/, "").split(".")[1] || "").length : 2;
  const price = fmt(Number(q.last_trade.price), dec);
  await page.waitForFunction((sym) => document.querySelector(".hero")?.textContent.includes(sym), ins.symbol);
  await page.waitForFunction((p) => document.querySelector(".hero .price")?.textContent.includes(p), price, { timeout: 10_000 }).catch(() => undefined);
  await page.waitForFunction(() => document.querySelectorAll(".obs").length > 0 || /ยังไม่มีเหตุการณ์ที่น่าสังเกต/.test(document.body.innerText), null, { timeout: 10_000 }).catch(() => undefined);
  const hero = await page.textContent(".hero");
  check(`${name} overview: instrument from the API (${ins.symbol} ${ins.market_type})`, hero.includes(ins.symbol), hero.slice(0, 90));
  check(`${name} overview: last price equals the API quote (${price} ${ins.quote_asset})`, hero.includes(`${price} ${ins.quote_asset}`));
  const ageS = (Date.now() - Date.parse(q.last_trade.at)) / 1000;
  const expectBadge = ageS <= 120 ? "ข้อมูลสด" : "ข้อมูลค้าง";
  check(`${name} overview: freshness badge follows the data time (${Math.round(ageS)} s old → ${expectBadge})`, hero.includes(expectBadge));
  const change = q.change_24h ? `${Number(q.change_24h.pct) >= 0 ? "+" : "−"}${fmt(Math.abs(Number(q.change_24h.pct)), 2)}%` : "ยังไม่มีข้อมูล";
  check(`${name} overview: 24 h change from the API (${change})`, hero.includes(change), hero.slice(0, 200));
  check(`${name} overview: no demo label in API mode`, (await page.locator("text=ข้อมูลตัวอย่าง").count()) === 0);
  const obs = await get(`/v1/instruments/${ins.instrument_id}/observations`);
  const shown = await page.locator(".obs").count();
  check(`${name} overview: observations from the API (${obs.items.map((o) => o.kind).join(", ") || "none"})`, shown === Math.min(3, obs.items.length), `${shown} shown`);
  await audit(page, `${name} overview`);
  await page.screenshot({ path: path.join(out, `${name}-overview.png`), fullPage: true });

  // analysis: chart bars, walls, big trades, OI/funding and quality all equal the API's answers
  await page.goto(`${APP}/#/analyze?id=${ins.instrument_id}`);
  await page.waitForSelector(".chart-box canvas");
  const tf = await page.locator('[aria-label="ขนาดแท่งเทียน"] [aria-checked=true]').textContent();
  const now = Date.now();
  const lb = { "6 ชม.": 6, "24 ชม.": 24, "3 วัน": 72 }[await page.locator('[aria-label="ช่วงข้อมูลย้อนหลัง"] [aria-checked=true]').textContent()];
  const bars = await get(`/v1/instruments/${ins.instrument_id}/bars?tf=${tf}&source=kline&from=${new Date(now - lb * 3600e3).toISOString()}&to=${new Date(now).toISOString()}`);
  await page.waitForFunction(() => /\d+ แท่ง/.test(document.querySelector(".chart-box")?.getAttribute("aria-label") ?? ""));
  const label = await page.getAttribute(".chart-box", "aria-label");
  const n = Number(label.match(/(\d+) แท่ง/)[1]);
  const priced = bars.bars.filter((b) => b.close !== null).length;
  check(`${name} analyze: chart has the API's ${tf} bars (${priced} over ${lb} h)`, Math.abs(n - priced) <= 1, label.slice(0, 120));
  const d = await get(`/v1/instruments/${ins.instrument_id}/details?from=${new Date(now - lb * 3600e3).toISOString()}&to=${new Date(now).toISOString()}`);
  for (const id of ["panel-walls", "panel-oi", "panel-quality"]) await page.evaluate((i) => document.getElementById(i)?.setAttribute("open", ""), id);
  await page.waitForTimeout(300);
  const walls = await page.locator("#panel-walls table.data").first().locator("tbody tr").count().catch(() => 0);
  const apiWalls = d.book?.walls.length ?? 0;
  check(`${name} analyze: walls listed as the API computed them (${apiWalls})`, apiWalls === 0 ? walls === 0 : walls === apiWalls, `${walls} rows`);
  const wallText = await page.textContent("#panel-walls");
  check(`${name} analyze: wall method stated`, d.book ? wallText.includes(d.book.method.slice(0, 20)) : true);
  check(`${name} analyze: big-trade method stated`, wallText.includes(d.big_trades.method.slice(0, 20)) || (await page.textContent("main")).includes(d.big_trades.method.slice(0, 20)));
  const oiText = await page.textContent("#panel-oi");
  if (d.oi === null && d.funding === null) check(`${name} analyze: OI/funding shown as not available for ${ins.market_type}`, oiText.includes("ตลาดนี้ไม่มี Open Interest"), oiText.slice(0, 120));
  else check(`${name} analyze: OI and funding rows from the API`, oiText.includes("Open Interest") && oiText.includes("Funding"), oiText.slice(0, 160));
  const qRows = await page.locator("#panel-quality table.data tr").count();
  const qText = await page.textContent("#panel-quality");
  check(`${name} analyze: data quality per stream from the API (${d.quality.map((x) => `${x.stream}=${x.status}`).join(", ")})`,
    qRows === d.quality.length && d.quality.every((x) => qText.includes(x.stream)), `${qRows} rows`);
  await audit(page, `${name} analyze`);
  await page.screenshot({ path: path.join(out, `${name}-analyze.png`), fullPage: true });
  await ctx.close();
}

// a member without a realtime entitlement sees delayed data and the screen says so
{
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "th-TH", timezoneId: "Asia/Bangkok" });
  await ctx.route(/fonts\.(googleapis|gstatic)\.com/, (r) => r.abort());
  const page = await ctx.newPage();
  page.on("pageerror", (e) => errors.push(`delayed: ${e.message}`));
  await connect(page, delayedToken);
  const list = await get("/v1/instruments", delayedToken);
  const q = await get(`/v1/instruments/${list.items[0].instrument_id}/quote`, delayedToken);
  await page.waitForFunction(() => document.querySelector(".hero")?.textContent.includes("หน่วง"));
  const hero = await page.textContent(".hero");
  check("delayed member: API access is delayed 15 min", q.access.level === "delayed" && q.access.delay_minutes === 15, JSON.stringify(q.access));
  check("delayed member: screen labels the price as delayed", /ข้อมูลหน่วง/.test(hero), hero.slice(0, 160));
  check("delayed member: shown price is at least 15 min old", !q.last_trade || Date.now() - Date.parse(q.last_trade.at) >= 15 * 60e3 - 5000);
  await page.screenshot({ path: path.join(out, "390-overview-delayed.png"), fullPage: true });
  await ctx.close();
}

// a wrong token is explained on screen, never an endless spinner or demo numbers
{
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "th-TH", timezoneId: "Asia/Bangkok" });
  await ctx.route(/fonts\.(googleapis|gstatic)\.com/, (r) => r.abort());
  const page = await ctx.newPage();
  await connect(page, "not-a-valid-token");
  const ok = await page.waitForSelector("[role=alert]:has-text('ต้องเข้าสู่ระบบใหม่')", { timeout: 10_000 }).then(() => true, () => false);
  check("wrong token: screen says to sign in again", ok);
  check("wrong token: no prices shown", (await page.locator(".hero .price").count()) === 0);
  await ctx.close();
}

check("app called the API endpoints", ["/v1/instruments", "/v1/instruments/{id}/quote", "/v1/instruments/{id}/bars", "/v1/instruments/{id}/details", "/v1/instruments/{id}/observations"].every((p) => apiCalls.has(p)), [...apiCalls].sort().join(" "));
check("app called no exchange or other host", otherHosts.size === 0, [...otherHosts].join(", "));
check("no page errors", errors.length === 0, errors.slice(0, 3).join(" | "));
await browser.close();
web.close();
stop();
const failed = results.filter((r) => !r.ok).length;
console.log(`\n${results.length - failed} passed, ${failed} failed · screenshots in ${out}`);
process.exit(failed ? 1 : 0);
