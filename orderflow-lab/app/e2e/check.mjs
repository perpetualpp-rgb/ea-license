// End-to-end check of the built app (npm run build first) in headless Chromium.
// Serves dist/ through request interception, walks every main flow and screenshots
// each page at 320, 390 and 1440 px. Run: NODE_PATH=$(npm root -g) node e2e/check.mjs
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const here = path.dirname(fileURLToPath(import.meta.url));
const dist = path.join(here, "..", "dist");
const out = process.env.OUT || path.join(here, "..", "..", "data", "app-e2e");
fs.mkdirSync(out, { recursive: true });
const ORIGIN = "https://flowlab.test"; // https so the page is a secure context like production
const TYPES = { ".html": "text/html", ".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".webmanifest": "application/manifest+json" };

const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok, detail }); console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? " — " + detail : ""}`); };

async function serve(ctx) {
  await ctx.route(/fonts\.(googleapis|gstatic)\.com/, (r) => r.abort());
  await ctx.route(`${ORIGIN}/**`, (r) => {
    const u = new URL(r.request().url());
    const file = path.join(dist, u.pathname === "/" ? "index.html" : u.pathname);
    if (!file.startsWith(dist) || !fs.existsSync(file)) return r.fulfill({ status: 404, body: "not found" });
    r.fulfill({ path: file, contentType: TYPES[path.extname(file)] || "application/octet-stream" });
  });
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

const browser = await chromium.launch({ executablePath: fs.existsSync("/opt/pw-browsers/chromium") ? "/opt/pw-browsers/chromium" : undefined }).catch(() => chromium.launch());
const errors = [];

for (const [name, viewport] of [["390", { width: 390, height: 844 }], ["320", { width: 320, height: 640 }], ["1440", { width: 1440, height: 900 }]]) {
  const ctx = await browser.newContext({ viewport, acceptDownloads: true, locale: "th-TH", timezoneId: "Asia/Bangkok" });
  await serve(ctx);
  const page = await ctx.newPage();
  page.on("pageerror", (e) => errors.push(`${name}: ${e.message}`));
  page.on("console", (m) => { if (m.type() === "error" && !/ERR_FAILED|net::/.test(m.text())) errors.push(`${name}: ${m.text()}`); });
  await page.goto(`${ORIGIN}/#/overview`);
  await page.waitForSelector(".hero .price");
  const hero = await page.textContent(".hero");
  check(`${name} overview: symbol, market, unit, quote and timezone are shown`, /BTCUSDT/.test(hero) && /Perpetual/.test(hero) && /หน่วย BTC/.test(hero) && /USDT/.test(hero) && /UTC\+7/.test(hero), hero.slice(0, 120));
  check(`${name} overview: demo data labelled`, (await page.locator("text=ข้อมูลตัวอย่าง").count()) > 0);
  await audit(page, `${name} overview`);
  await page.screenshot({ path: path.join(out, `${name}-overview.png`), fullPage: true });
  if (name !== "390") { await ctx.close(); continue; }

  // gold is never presented as COMEX/XAUUSD
  await page.click('.chip:has-text("XAUUSDT")');
  await page.waitForSelector("text=ไม่ใช่ COMEX GC");
  check("XAUUSDT shows the not-COMEX caution", true);
  await page.click('.chip:has-text("BTCUSDT") >> nth=0');

  // evidence link opens analysis with the right panel
  await page.click(".obs >> nth=1");
  await page.waitForSelector(".chart-box canvas");
  check("observation opens analysis with OI panel open", await page.evaluate(() => document.getElementById("panel-oi")?.hasAttribute("open")));
  await page.click('[role=radio]:has-text("1h")');
  await page.click('[role=radio]:has-text("3 วัน")');
  await page.waitForTimeout(300);
  check("timeframe and lookback are separate controls", (await page.locator('[aria-label="ขนาดแท่งเทียน"] [aria-checked=true]').textContent()) === "1h" &&
    (await page.locator('[aria-label="ช่วงข้อมูลย้อนหลัง"] [aria-checked=true]').textContent()) === "3 วัน");
  // the desk's chart layers: walls, big trades, OI pane on by default; Assist, Range, round numbers on demand
  const chips = await page.locator('[aria-label="ชั้นข้อมูลบนกราฟ"] .toggle').allTextContents();
  check("layer buttons match the desk", ["กำแพง", "ไม้ใหญ่", "VWAP", "Assist", "POC", "Range", "เลขกลม", "CVD", "OI"].every((x) => chips.some((c) => c.includes(x))), chips.join(" | "));
  await page.waitForFunction(() => Number(document.querySelector(".chart-box")?.dataset.walls) > 0, null, { timeout: 8000 }).catch(() => undefined);
  let ds = await page.evaluate(() => ({ ...document.querySelector(".chart-box").dataset }));
  check("chart draws walls, big-trade markers and the OI pane", +ds.walls > 0 && +ds.big > 0 && +ds.oi > 0, JSON.stringify(ds));
  for (const name of ["Assist", "Range", "เลขกลม"]) await page.click(`[aria-label="ชั้นข้อมูลบนกราฟ"] .toggle:has-text("${name}")`);
  await page.waitForTimeout(300);
  ds = await page.evaluate(() => ({ ...document.querySelector(".chart-box").dataset }));
  check("Range (previous day, 3-day view) and round numbers are drawn when switched on", +ds.range === 5 && +ds.round > 0, JSON.stringify(ds));
  check("chart key explains every visible layer", /กำแพง \(ทึบ/.test(await page.textContent(".chart-key")) && /Range วันก่อนหน้า/.test(await page.textContent(".chart-key")));
  await page.click('button:has-text("แท่งล่าสุด")');
  await page.click('button:has-text("รีเซ็ตมุมมอง")');
  for (const name of ["Assist", "Range", "เลขกลม"]) await page.click(`[aria-label="ชั้นข้อมูลบนกราฟ"] .toggle:has-text("${name}")`);
  const caption = await page.textContent(".caption");
  check("CVD caption names anchor and unit", /07:00 UTC\+7/.test(caption) && /หน่วย BTC/.test(caption), caption.slice(0, 100));
  await page.click('button[aria-label="อธิบาย CVD"]');
  await page.waitForSelector('[role=dialog] >> text=ไม่ใช่เงินไหลเข้าสุทธิ');
  check("glossary sheet explains CVD", true);
  await page.keyboard.press("Escape");
  await page.waitForSelector("[role=dialog]", { state: "detached" });
  await audit(page, "390 analyze");
  await page.screenshot({ path: path.join(out, "390-analyze.png"), fullPage: true });

  // spot has no OI: shown as unavailable, never as 0
  await page.selectOption("#asset-pick", "binance-spot-btcusdt");
  await page.waitForTimeout(400);
  await page.click('summary:has-text("OI และ Funding")');
  check("spot OI shown as not available (not zero)", (await page.locator("text=ตลาดนี้ไม่มี Open Interest").count()) === 1);
  await page.selectOption("#asset-pick", "binance-um-btcusdt");
  await page.waitForTimeout(400);

  // market snapshot → journal, survives reload, review creates a revision
  await page.click('.mobile-only button:has-text("บันทึกภาพตลาด")');
  await page.waitForSelector("[role=dialog] img.entry-thumb");
  await page.fill('[role=dialog] textarea >> nth=0', "CVD เพิ่มขึ้นขณะราคายืนเหนือ VWAP");
  await page.click('[role=dialog] button:has-text("CVD")');
  await page.click('[role=dialog] button[type=submit]');
  await page.waitForSelector(".toast");
  check("snapshot saved as a local draft", /ร่างในเครื่อง/.test(await page.textContent(".toast")));
  await page.goto(`${ORIGIN}/#/journal`);
  await page.reload();
  await page.waitForSelector("button.card img.entry-thumb");
  check("entry with chart image survives a reload", (await page.locator("button.card").count()) === 1);
  await page.click("button.card");
  await page.waitForSelector("[role=dialog] >> text=ภาพตลาด ณ");
  await page.fill('[role=dialog] textarea', "ราคาไม่ไปต่อหลัง CVD พุ่ง");
  await page.click('[role=dialog] button:has-text("บันทึกบททบทวน")');
  await page.waitForSelector("[role=dialog] >> text=ประวัติการแก้ไข (1)");
  check("review is stored as a revision", true);
  await page.keyboard.press("Escape");

  // personal trade with partial exits
  await page.click('[role=tab]:has-text("เทรดส่วนตัว")');
  await page.click('button:has-text("เพิ่มเทรดที่ฉันทำเอง")');
  await page.fill('[role=dialog] input[placeholder="ไม่บังคับ"]', "20");
  for (const [qty, price, fee] of [["2", "100", "0.2"], ["1", "110", "0.11"], ["1", "100", "0.1"]]) {
    await page.click('[role=dialog] button:has-text("เพิ่ม fill")');
    const row = page.locator("[role=dialog] .fill-row").last();
    await row.locator("input >> nth=0").fill(qty);
    await row.locator("input >> nth=1").fill(price);
    await row.locator("input >> nth=2").fill(fee);
  }
  const prev = await page.textContent("[role=dialog] .card.small");
  check("trade preview: net after fees and R", /สุทธิ 9\.59/.test(prev) && /R 0\.48/.test(prev), prev.replace(/\s+/g, " ").slice(0, 160));
  await page.click('[role=dialog] button[type=submit]');
  await page.waitForSelector(".tiles >> text=100%");
  check("win rate counts the closed trade", true);
  await audit(page, "390 journal");
  await page.screenshot({ path: path.join(out, "390-journal.png"), fullPage: true });

  // export
  const [dl] = await Promise.all([page.waitForEvent("download"), page.click('button:has-text("JSON ทั้งหมด")')]);
  const json = JSON.parse(fs.readFileSync(await dl.path(), "utf8"));
  check("JSON export holds the entry, its revision and the trade", json.entries.length === 1 && json.entries[0].revisions.length === 1 && json.trades.length === 1);

  // alerts: rule with measured state; a wall rule on gold pauses because the book is resyncing
  await page.goto(`${ORIGIN}/#/alerts`);
  await page.waitForSelector("text=OI เปลี่ยนแปลง");
  check("alert history shows measured value vs threshold", (await page.locator("text=เกณฑ์ 2").count()) > 0);
  check("backfilled event is marked as not live", (await page.locator("text=ย้อนหลัง · ไม่ได้แจ้งสด").count()) === 1);
  await page.click('button:has-text("สร้างการแจ้งเตือน")');
  await page.selectOption("[role=dialog] select >> nth=0", "binance-um-xauusdt");
  await page.selectOption("[role=dialog] select >> nth=1", "wall_size");
  await page.fill('[role=dialog] input[type=number]', "500");
  await page.click('[role=dialog] button[type=submit]');
  await page.waitForSelector("text=พักชั่วคราว");
  check("rule on a resyncing book is paused, not fired", true);
  await audit(page, "390 alerts");
  await page.screenshot({ path: path.join(out, "390-alerts.png"), fullPage: true });

  // keyboard: sheets trap focus and Escape closes them
  await page.click('button:has-text("สร้างการแจ้งเตือน")');
  const inDialog = await page.evaluate(() => !!document.activeElement?.closest("[role=dialog]"));
  await page.keyboard.press("Escape");
  check("dialog takes focus and closes with Escape", inDialog && (await page.locator("[role=dialog]").count()) === 0);
  await ctx.close();
}

// desktop layout: side navigation and analysis side panel
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: "th-TH", timezoneId: "Asia/Bangkok" });
  await serve(ctx);
  const page = await ctx.newPage();
  page.on("pageerror", (e) => errors.push(`1440 analyze: ${e.message}`));
  await page.goto(`${ORIGIN}/#/analyze?id=binance-um-btcusdt`);
  await page.waitForSelector(".chart-box canvas");
  check("1440: side navigation visible, bottom nav hidden", await page.isVisible(".side-nav") && !(await page.isVisible(".bottom-nav")));
  check("1440: analysis has a context panel beside the chart", await page.evaluate(() => { const a = document.querySelector(".an-side").getBoundingClientRect(), c = document.querySelector(".chart-box").getBoundingClientRect(); return a.left > c.right; }));
  await audit(page, "1440 analyze");
  await page.screenshot({ path: path.join(out, "1440-analyze.png"), fullPage: true });
  for (const p of ["alerts", "journal"]) { await page.goto(`${ORIGIN}/#/${p}`); await page.waitForTimeout(500); await page.screenshot({ path: path.join(out, `1440-${p}.png`), fullPage: true }); }
  await ctx.close();
}

await browser.close();
check("no page errors", errors.length === 0, errors.slice(0, 5).join(" | "));
const failed = results.filter((r) => !r.ok).length;
console.log(`\n${results.length - failed} passed, ${failed} failed · screenshots in ${out}`);
process.exit(failed ? 1 : 0);
