// Opens the dashboard in headless Chromium, waits for live data, reports console
// errors and saves screenshots (light + dark, desktop + phone). Dev tool, not a
// unit test: needs `playwright` (npm) and network access to the exchanges.
// The page itself is served from the repo on disk (no http server needed); only
// the exchange calls hit the network, through HTTPS_PROXY when that is set.
//   node tests/render_check.js [outdir]
"use strict";
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..", "..");  // repo root: /orderflow-lab/web/ maps onto it
const ORIGIN = "http://oflab.local";
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "application/javascript", ".json": "application/json",
                ".jsonl": "application/x-ndjson", ".css": "text/css", ".png": "image/png" };

(async () => {
  const out = process.argv[2] || path.join(__dirname, "..", "data", "shots");
  fs.mkdirSync(out, { recursive: true });
  const proxy = process.env.HTTPS_PROXY || process.env.https_proxy;
  const browser = await chromium.launch(proxy ? { proxy: { server: proxy } } : {});
  const errors = [];
  let failures = 0;
  for (const [name, opts] of [["desktop-light", { viewport: { width: 1280, height: 1100 }, colorScheme: "light" }],
                              ["desktop-dark", { viewport: { width: 1280, height: 1100 }, colorScheme: "dark" }],
                              ["phone-light", { viewport: { width: 390, height: 844 }, colorScheme: "light" }]]) {
    const ctx = await browser.newContext({ ...opts, ignoreHTTPSErrors: !!proxy });
    await ctx.route(u => u.href.startsWith(ORIGIN), route => {
      let p = new URL(route.request().url()).pathname;
      if (p.endsWith("/")) p += "index.html";
      const file = path.join(ROOT, p);
      if (!file.startsWith(ROOT) || !fs.existsSync(file)) return route.fulfill({ status: 404, body: "not found: " + p });
      return route.fulfill({ status: 200, contentType: TYPES[path.extname(file)] || "application/octet-stream", body: fs.readFileSync(file) });
    });
    const url = ORIGIN + "/orderflow-lab/web/";
    const page = await ctx.newPage();
    page.on("pageerror", e => errors.push(`${name}: pageerror ${e.message}`));
    page.on("console", m => { if (m.type() === "error") errors.push(`${name}: console ${m.text()}`); });
    await page.goto(url, { waitUntil: "load" });
    console.log(`[${name}] loaded "${await page.title()}"`);
    await page.waitForTimeout(12000); // two polls
    const status = await page.textContent("#status");
    const last = await page.textContent("#tLast");
    const scrollW = await page.evaluate(() => document.documentElement.scrollWidth);
    const ok = /●/.test(status) && last !== "–";
    if (!ok) failures += 1;
    console.log(`[${name}] status=${status.trim()} | last=${last} | scrollWidth=${scrollW}/${opts.viewport.width}`);
    await page.screenshot({ path: path.join(out, `${name}.png`), fullPage: true });
    await ctx.close();
  }
  await browser.close();
  if (errors.length) { console.log("ERRORS:\n" + errors.join("\n")); process.exit(1); }
  console.log(failures ? `done with ${failures} failing viewport(s)` : "done, no console errors");
  process.exit(failures ? 1 : 0);
})();
