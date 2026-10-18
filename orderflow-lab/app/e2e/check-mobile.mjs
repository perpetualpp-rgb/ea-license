// Phone usability of the analysis chart, on emulated phones with real touch events (CDP):
// the chart is on the first screen, nothing covers it, a finger drag pans and a pinch zooms,
// the toolbar works without gestures, and full screen gives the chart the whole viewport.
// Run after `npm run build`: NODE_PATH=$(npm root -g) node e2e/check-mobile.mjs
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium, devices } = require("playwright");
const here = path.dirname(fileURLToPath(import.meta.url));
const dist = path.join(here, "..", "dist");
const out = process.env.OUT || path.join(here, "..", "..", "data", "app-e2e-mobile");
fs.mkdirSync(out, { recursive: true });
const ORIGIN = "https://flowlab.test";
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok }); console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? " — " + detail : ""}`); };

// a Claude-app-like viewport is shorter: the host shows its own header above the page
const PHONES = [
  ["pixel7", { ...devices["Pixel 7"] }],
  ["in-app-412x640", { ...devices["Pixel 7"], viewport: { width: 412, height: 640 } }],
  ["iphone-se-375x667", { ...devices["Pixel 7"], viewport: { width: 375, height: 667 }, deviceScaleFactor: 2 }],
  ["small-320x568", { ...devices["Pixel 7"], viewport: { width: 320, height: 568 }, deviceScaleFactor: 2 }],
];

const browser = await chromium.launch({ executablePath: fs.existsSync("/opt/pw-browsers/chromium") ? "/opt/pw-browsers/chromium" : undefined }).catch(() => chromium.launch());
const errors = [];
for (const [name, dev] of PHONES) {
  const ctx = await browser.newContext({ ...dev, locale: "th-TH", timezoneId: "Asia/Bangkok" });
  await ctx.route(/fonts\.(googleapis|gstatic)\.com/, (r) => r.abort());
  await ctx.route(`${ORIGIN}/**`, (r) => {
    const u = new URL(r.request().url());
    const f = path.join(dist, u.pathname === "/" ? "index.html" : u.pathname);
    if (!f.startsWith(dist) || !fs.existsSync(f)) return r.fulfill({ status: 404, body: "" });
    r.fulfill({ path: f });
  });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => errors.push(`${name}: ${e.message}`));
  const cdp = await ctx.newCDPSession(page);
  const touch = (type, pts) => cdp.send("Input.dispatchTouchEvent", { type, touchPoints: pts.map(([x, y], id) => ({ x, y, id })) });
  const drag = async (from, to, steps = 10) => {
    await touch("touchStart", [from]);
    for (let i = 1; i <= steps; i++) { await touch("touchMove", [[from[0] + (to[0] - from[0]) * i / steps, from[1] + (to[1] - from[1]) * i / steps]]); await page.waitForTimeout(16); }
    await touch("touchEnd", []);
    await page.waitForTimeout(250);
  };
  const range = async () => page.evaluate(() => { const d = document.querySelector(".chart-wrap:not(.x) .chart-box").dataset; return { from: +d.from, to: +d.to }; });
  const vh = page.viewportSize().height, vw = page.viewportSize().width;

  await page.goto(`${ORIGIN}/#/analyze?id=binance-um-xauusdt`);
  await page.waitForSelector(".chart-box canvas");
  await page.waitForTimeout(600);

  // 1. the chart is on the first screen
  const top = await page.evaluate(() => document.querySelector(".chart-box").getBoundingClientRect().top + scrollY);
  const fsBtn = await page.evaluate(() => document.querySelector('button[aria-label="ขยายกราฟเต็มจอ"]').getBoundingClientRect().bottom);
  check(`${name}: chart starts on the first screen and ⤢ full screen is reachable without scrolling`, top + 60 <= vh && fsBtn <= vh - 56,
    `chart top ${Math.round(top)} px, ⤢ bottom ${Math.round(fsBtn)} px, screen ${vh} px`);

  // 2. nothing covers the chart once it is scrolled into view
  await page.evaluate(() => document.querySelector(".chart-wrap").scrollIntoView({ block: "start" }));
  await page.waitForTimeout(200);
  const cover = await page.evaluate(() => {
    const c = document.querySelector(".chart-box").getBoundingClientRect();
    const hits = [];
    for (const el of document.querySelectorAll("body *")) {
      const st = getComputedStyle(el);
      if (!["fixed", "sticky"].includes(st.position) && !el.matches(".chart-readout, .chart-bar")) continue;
      if (el.closest(".chart-box") || st.visibility === "hidden" || st.display === "none") continue;
      const r = el.getBoundingClientRect();
      const ox = Math.max(0, Math.min(r.right, c.right) - Math.max(r.left, c.left)), oy = Math.max(0, Math.min(r.bottom, c.bottom) - Math.max(r.top, c.top));
      if (ox * oy > 4) hits.push(`${el.tagName.toLowerCase()}.${el.className}`.slice(0, 40) + ` ${Math.round(oy)}px`);
    }
    return hits;
  });
  check(`${name}: nothing (readout, toolbar, nav, buttons) covers the chart`, cover.length === 0, cover.join(", "));
  const geo = await page.evaluate(() => {
    const c = document.querySelector(".chart-box").getBoundingClientRect();
    let top = 0, bottom = innerHeight;
    for (const el of document.querySelectorAll("body *")) {
      const st = getComputedStyle(el);
      if (st.position !== "fixed" || st.display === "none") continue;
      const r = el.getBoundingClientRect();
      if (r.height > innerHeight * 0.5 || r.width < innerWidth * 0.5) continue;
      if (r.top <= 0 && r.bottom > top) top = r.bottom;
      if (r.bottom >= innerHeight - 1 && r.top < bottom) bottom = r.top;
    }
    return { chartH: c.height, free: bottom - top };
  });
  check(`${name}: the whole chart fits between the fixed bars`, geo.chartH <= geo.free + 1, `chart ${Math.round(geo.chartH)} px, free ${Math.round(geo.free)} px`);
  check(`${name}: chart is the largest element (≥ 45 % of the screen)`, geo.chartH >= 0.45 * vh || geo.chartH >= 290, `${Math.round(geo.chartH)} / ${vh}`);
  const small = await page.evaluate(() => [...document.querySelectorAll(".chart-bar button")].filter((b) => { const r = b.getBoundingClientRect(); return r.width > 0 && (r.width < 44 || r.height < 44); }).length);
  check(`${name}: chart buttons are ≥ 44 px`, small === 0);

  // 3. a finger drag pans, a pinch zooms, and the page does not scroll sideways
  const b = await page.locator(".chart-box").boundingBox();
  const y = b.y + b.height * 0.3, x = b.x + b.width * 0.55;
  let r0 = await range();
  await drag([x, y], [x - 140, y]);
  let r1 = await range();
  check(`${name}: finger drag pans the chart`, r1.from < r0.from - 3 || r1.from > r0.from + 3, `${r0.from}→${r1.from}`);
  await touch("touchStart", [[x - 30, y], [x + 30, y]]);
  for (let i = 1; i <= 10; i++) { await touch("touchMove", [[x - 30 - i * 9, y], [x + 30 + i * 9, y]]); await page.waitForTimeout(16); }
  await touch("touchEnd", []);
  await page.waitForTimeout(250);
  const r2 = await range();
  check(`${name}: pinch zooms the chart`, (r2.to - r2.from) < (r1.to - r1.from) * 0.9, `${(r1.to - r1.from).toFixed(0)}→${(r2.to - r2.from).toFixed(0)} bars`);
  check(`${name}: no sideways page scroll`, await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));

  // 4. the toolbar does the same without gestures (for apps that keep gestures for themselves)
  r0 = await range();
  await page.click('button[aria-label="ซูมออก"]');
  await page.waitForTimeout(300);
  r1 = await range();
  await page.click('button[aria-label="เลื่อนไปแท่งก่อนหน้า"]');
  await page.waitForTimeout(300);
  const r3 = await range();
  await page.click('button[aria-label="ไปแท่งล่าสุด"]');
  await page.waitForTimeout(700);
  const r4 = await range();
  const n = await page.evaluate(() => Number(document.querySelector(".chart-box").getAttribute("aria-label").match(/(\d+) แท่ง/)[1]));
  check(`${name}: − zooms out, ◀ moves back, ⇥ returns to the newest bar`,
    r1.to - r1.from > r0.to - r0.from + 1 && r3.to < r1.to - 1 && r4.to >= n - 1, `${JSON.stringify([r0, r1, r3, r4])} n=${n}`);
  await page.screenshot({ path: path.join(out, `${name}-analyze.png`) });

  // 5. full screen: the chart gets the viewport and every drag goes to the chart
  await page.click('button[aria-label="ขยายกราฟเต็มจอ"]');
  await page.waitForTimeout(400);
  const fs1 = await page.evaluate(() => {
    const w = document.querySelector(".chart-wrap.full")?.getBoundingClientRect();
    const c = document.querySelector(".chart-wrap.full .chart-box")?.getBoundingClientRect();
    return w && c ? { cover: (w.width * w.height) / (innerWidth * innerHeight), chartH: c.height, y: c.top + c.height * 0.4, x: c.left + c.width * 0.5 } : null;
  });
  check(`${name}: full screen covers the screen`, !!fs1 && fs1.cover >= 0.98, fs1 ? `${Math.round(fs1.cover * 100)} %` : "no full-screen chart");
  check(`${name}: full-screen chart uses ≥ 55 % of the height`, !!fs1 && fs1.chartH >= 0.55 * vh, fs1 ? `${Math.round(fs1.chartH)} / ${vh}` : "");
  if (fs1) {
    const sy = await page.evaluate(() => scrollY);
    const ra = await range();
    await drag([fs1.x, fs1.y], [fs1.x - 100, fs1.y + 120]);
    const rb = await range();
    check(`${name}: full screen: a diagonal drag moves the chart, not the page`, (await page.evaluate(() => scrollY)) === sy && Math.abs(rb.from - ra.from) > 2, `scroll ${sy}, from ${ra.from}→${rb.from}`);
    await page.screenshot({ path: path.join(out, `${name}-fullscreen.png`) });
    await page.keyboard.press("Escape");
    await page.waitForTimeout(200);
    check(`${name}: Escape leaves full screen`, (await page.locator(".chart-wrap.full").count()) === 0);
  }
  await ctx.close();
}
check("no page errors", errors.length === 0, errors.slice(0, 3).join(" | "));
await browser.close();
const failed = results.filter((r) => !r.ok).length;
console.log(`\n${results.length - failed} passed, ${failed} failed · screenshots in ${out}`);
process.exit(failed ? 1 : 0);
