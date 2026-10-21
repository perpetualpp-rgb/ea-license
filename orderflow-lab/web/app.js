/* Order Flow Lab dashboard. Static page: fetches public exchange APIs straight
 * from the browser (CORS verified for OKX, Kraken, Coinbase), runs engine.js on
 * the accumulated trades/books and draws everything on <canvas>. No server,
 * no API keys, nothing leaves the viewer's browser except the exchange calls. */
"use strict";
(() => {
  const SOURCES = [
    { key: "okx:BTC-USDT", exchange: "okx", symbol: "BTC-USDT", label: "BTC · OKX", tick: 10, roundStep: 500, dec: 1, bookEvery: 5000 },
    { key: "kraken:XBTUSD", exchange: "kraken", symbol: "XBTUSD", label: "BTC · Kraken", tick: 10, roundStep: 500, dec: 1, bookEvery: 5000 },
    { key: "coinbase:BTC-USD", exchange: "coinbase", symbol: "BTC-USD", label: "BTC · Coinbase", tick: 10, roundStep: 500, dec: 2, bookEvery: 20000 },
    { key: "okx:XAUT-USDT", exchange: "okx", symbol: "XAUT-USDT", label: "ทอง (โทเคน XAUT) · OKX", tick: 0.5, roundStep: 25, dec: 2, bookEvery: 5000, proxy: true },
    { key: "kraken:PAXGUSD", exchange: "kraken", symbol: "PAXGUSD", label: "ทอง (โทเคน PAXG) · Kraken", tick: 0.5, roundStep: 25, dec: 2, bookEvery: 5000, proxy: true },
    { key: "coinbase:PAXG-USD", exchange: "coinbase", symbol: "PAXG-USD", label: "ทอง (โทเคน PAXG) · Coinbase", tick: 0.5, roundStep: 25, dec: 2, bookEvery: 20000, proxy: true },
  ];
  const POLL_MS = 5000, KEEP_MS = 4 * 3600 * 1000, BOOK_DEPTH = 200;
  const TH = {
    status: { new: "ใหม่", holding: "ยืน", adding: "เติม", pulling: "ถอน", price_reached: "ราคาไปถึง", removed: "หายไปก่อนราคาถึง", unknown: "ไม่ทราบ" },
    pos: { above: "เหนือ VWAP", below: "ใต้ VWAP", expensive: "แพง (เกิน +2σ)", cheap: "ถูก (ต่ำกว่า −2σ)", at_vwap: "ที่ VWAP" },
    shape: { flat: "แถบแบน · ตลาดแกว่ง", expanding: "แถบขยายเร็ว · อย่าสวน", widening: "แถบกว้างขึ้น" },
    div: { bearish: "ราคาทำจุดสูงใหม่ แต่ CVD ไม่ตาม", bullish: "ราคาทำจุดต่ำใหม่ แต่ CVD ไม่ตาม" },
  };

  // ---- storage (per-viewer conveniences only) -------------------------------
  const store = {
    get(k, d) { try { const v = localStorage.getItem("oflab." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("oflab." + k, JSON.stringify(v)); } catch { /* private mode etc. */ } },
  };

  // ---- feeds (mirror feeds/*.py) --------------------------------------------
  async function getJSON(url) {
    const r = await fetch(url, { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  }
  const isoMs = s => { s = s.replace(/Z$/, ""); const [head, frac = ""] = s.split("."); return Date.parse(head + "Z") + Number((frac + "000").slice(0, 3)); };
  const byTime = (a, b) => a.ts - b.ts || (Number(a.trade_id) - Number(b.trade_id)) || 0;
  const FEEDS = {
    okx: {
      async trades(sym) {
        const r = await getJSON(`https://www.okx.com/api/v5/market/trades?instId=${sym}&limit=500`);
        if (String(r.code) !== "0") throw new Error(`okx ${r.code}: ${r.msg}`);
        return r.data.map(d => ({ ts: +d.ts, price: +d.px, size: +d.sz, side: d.side, exchange: "okx", symbol: sym, trade_id: String(d.tradeId) })).sort(byTime);
      },
      async book(sym) {
        const r = await getJSON(`https://www.okx.com/api/v5/market/books?instId=${sym}&sz=${Math.min(BOOK_DEPTH, 400)}`);
        if (String(r.code) !== "0") throw new Error(`okx ${r.code}: ${r.msg}`);
        const b = r.data[0], s = OF.sortBook(b.bids.map(l => [+l[0], +l[1]]), b.asks.map(l => [+l[0], +l[1]]));
        return { ts: +b.ts, exchange: "okx", symbol: sym, ...s };
      },
    },
    kraken: {
      async trades(sym, st) {
        const r = await getJSON(`https://api.kraken.com/0/public/Trades?pair=${sym}&count=1000${st.cursor ? "&since=" + st.cursor : ""}`);
        if (r.error && r.error.length) throw new Error("kraken: " + r.error.join(", "));
        st.cursor = r.result.last;
        const rows = (Object.entries(r.result).find(([k]) => k !== "last") || [null, []])[1];
        return rows.map(row => ({ ts: Math.trunc(row[2] * 1000), price: +row[0], size: +row[1], side: row[3] === "b" ? "buy" : "sell",
                                  exchange: "kraken", symbol: sym, trade_id: String(row[6] ?? Math.trunc(row[2] * 1e6)) })).sort(byTime);
      },
      async book(sym) {
        const r = await getJSON(`https://api.kraken.com/0/public/Depth?pair=${sym}&count=${Math.min(BOOK_DEPTH, 500)}`);
        if (r.error && r.error.length) throw new Error("kraken: " + r.error.join(", "));
        const b = Object.values(r.result)[0];
        const ts = Math.max(0, ...b.bids.concat(b.asks).map(l => +l[2])) * 1000;
        return { ts, exchange: "kraken", symbol: sym, ...OF.sortBook(b.bids.map(l => [+l[0], +l[1]]), b.asks.map(l => [+l[0], +l[1]])) };
      },
    },
    coinbase: {
      async trades(sym) {
        const r = await getJSON(`https://api.exchange.coinbase.com/products/${sym}/trades?limit=1000`);
        // Coinbase "side" is the MAKER side -> invert to taker side like every other feed
        return r.map(d => ({ ts: isoMs(d.time), price: +d.price, size: +d.size, side: d.side === "sell" ? "buy" : "sell",
                             exchange: "coinbase", symbol: sym, trade_id: String(d.trade_id) })).sort(byTime);
      },
      async book(sym) {
        const r = await getJSON(`https://api.exchange.coinbase.com/products/${sym}/book?level=2`);
        const s = OF.sortBook(r.bids.map(l => [+l[0], +l[1]]), r.asks.map(l => [+l[0], +l[1]]));
        return { ts: r.time ? isoMs(r.time) : Date.now(), exchange: "coinbase", symbol: sym, bids: s.bids.slice(0, BOOK_DEPTH), asks: s.asks.slice(0, BOOK_DEPTH) };
      },
    },
  };

  // ---- state -----------------------------------------------------------------
  const states = new Map();
  function stateFor(src) {
    if (!states.has(src.key)) states.set(src.key, { trades: [], ids: new Set(), books: 0, lastBook: null, lastBookAt: 0, cursor: null,
      tracker: new OF.WallTracker(src.tick, { mergeGap: src.tick }), error: null, updated: 0 });
    return states.get(src.key);
  }
  const ui = {
    source: store.get("source", SOURCES[0].key), windowMin: store.get("window", 60), interval: store.get("interval", "1m"), hover: null,
  };
  const currentSource = () => SOURCES.find(s => s.key === ui.source) || SOURCES[0];
  const intervalMs = () => ({ "1m": 60000, "5m": 300000, "15m": 900000 })[ui.interval];

  async function poll() {
    if (document.hidden) return;
    const src = currentSource(), st = stateFor(src);
    try {
      const fresh = (await FEEDS[src.exchange].trades(src.symbol, st)).filter(t => !st.ids.has(t.trade_id));
      for (const t of fresh) { st.ids.add(t.trade_id); st.trades.push(t); }
      if (fresh.length) st.trades.sort(byTime);
      const cutoff = Date.now() - KEEP_MS;
      if (st.trades.length && st.trades[0].ts < cutoff) { st.trades = st.trades.filter(t => t.ts >= cutoff); st.ids = new Set(st.trades.map(t => t.trade_id)); }
      if (Date.now() - st.lastBookAt >= src.bookEvery) {
        st.lastBook = await FEEDS[src.exchange].book(src.symbol);
        st.tracker.update(st.lastBook); st.books += 1; st.lastBookAt = Date.now();
      }
      st.error = null;
    } catch (e) { st.error = e.message || String(e); }
    st.updated = Date.now();
    render();
  }

  // ---- formatting ------------------------------------------------------------
  const $ = id => document.getElementById(id);
  const fmtP = (p, dec) => p == null ? "–" : p.toLocaleString("en-US", { minimumFractionDigits: dec, maximumFractionDigits: dec });
  const fmtS = s => s == null ? "–" : Math.abs(s) >= 100 ? s.toFixed(1) : Math.abs(s) >= 1 ? s.toFixed(3) : s.toFixed(4);
  const fmtSigned = s => (s > 0 ? "+" : "") + fmtS(s);
  const fmtT = (sec) => new Date(sec * 1000).toLocaleTimeString("th-TH", { hour: "2-digit", minute: "2-digit" });
  const fmtAge = ms => ms >= 3600000 ? (ms / 3600000).toFixed(1) + " ชม." : ms >= 60000 ? Math.round(ms / 60000) + " นาที" : Math.round(ms / 1000) + " วิ";
  function tokens() {
    const c = getComputedStyle(document.documentElement), g = n => c.getPropertyValue(n).trim();
    return { surface: g("--surface"), ink: g("--ink"), ink2: g("--ink-2"), muted: g("--muted"), grid: g("--grid"), axis: g("--axis"),
             buy: g("--buy"), sell: g("--sell"), vwap: g("--vwap"), bandA: g("--band-a"), bandB: g("--band-b"), va: g("--va"), magnet: g("--magnet") };
  }

  // ---- canvas helpers --------------------------------------------------------
  function sizeCanvas(cv) {
    const dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }
  function niceTicks(lo, hi, n = 6) {
    const span = hi - lo || 1, raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= n) || 10 * mag;
    const out = []; for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(Number(v.toFixed(10)));
    return out;
  }
  const hline = (ctx, x0, x1, y, color, width = 1, dash = null) => { ctx.save(); ctx.strokeStyle = color; ctx.lineWidth = width; if (dash) ctx.setLineDash(dash); ctx.beginPath(); ctx.moveTo(x0, Math.round(y) + 0.5); ctx.lineTo(x1, Math.round(y) + 0.5); ctx.stroke(); ctx.restore(); };

  /* Price chart: candles + VWAP bands + round levels + big trades, a buy|sell
   * profile strip sharing the y-axis, and a walls strip on the right. */
  function drawPriceChart(cv, S, o) {
    const { ctx, w, h } = sizeCanvas(cv), T = tokens(), bars = S.bar_list;
    if (!bars || !bars.length) return;
    const padL = 58, padT = 10, padB = 22;
    const wallsW = o.walls ? Math.min(150, Math.max(84, w * 0.16)) : 0, profW = Math.min(170, Math.max(80, w * 0.2));
    const plot = { l: padL, r: w - profW - wallsW - 6, t: padT, b: h - padB };
    const prof = { l: plot.r + 6, r: plot.r + 6 + profW - 6 }, walls = { l: w - wallsW, r: w - 2 };
    let lo = Math.min(...bars.map(b => b.l)), hi = Math.max(...bars.map(b => b.h));
    for (const wl of (o.walls || [])) { lo = Math.min(lo, wl.price); hi = Math.max(hi, wl.price); }
    if (S.last != null) { lo = Math.min(lo, S.last); hi = Math.max(hi, S.last); }
    const pad = (hi - lo || 1) * 0.04; lo -= pad; hi += pad;
    const yOf = p => plot.b - (p - lo) / (hi - lo) * (plot.b - plot.t);
    const n = bars.length, bw = Math.min(14, (plot.r - plot.l) / n), xOf = i => plot.r - (n - 1 - i + 0.5) * bw;

    // VWAP ±σ bands (2σ then 1σ) — magnitude from the VWAP hue, translucent
    const vs = S.vwap_series || [];
    if (vs.length === n) for (const [k, col] of [[2, T.bandA], [1, T.bandB]]) {
      ctx.fillStyle = col; ctx.beginPath();
      vs.forEach((p, i) => { const y = yOf(p.bands[k][1]); i ? ctx.lineTo(xOf(i), y) : ctx.moveTo(xOf(i), y); });
      for (let i = n - 1; i >= 0; i--) ctx.lineTo(xOf(i), yOf(vs[i].bands[k][0]));
      ctx.closePath(); ctx.fill();
    }
    // grid + y labels
    ctx.font = "11px system-ui, -apple-system, 'Segoe UI', sans-serif"; ctx.textBaseline = "middle";
    for (const v of niceTicks(lo, hi, 6)) { hline(ctx, plot.l, walls.r, yOf(v), T.grid); ctx.fillStyle = T.muted; ctx.textAlign = "right"; ctx.fillText(fmtP(v, o.dec), padL - 6, yOf(v)); }
    // round levels (reference thresholds, so dashed is the honest cue)
    for (const v of (S.round_levels || [])) if (v > lo && v < hi) { hline(ctx, plot.l, prof.r, yOf(v), T.axis, 1, [4, 4]); }
    // value area / magnet shading in the profile strip
    if (S.range) {
      ctx.fillStyle = T.va; ctx.fillRect(prof.l, yOf(S.range.va_high) - 1, prof.r - prof.l, yOf(S.range.va_low) - yOf(S.range.va_high) + 2);
      if (S.magnet) { ctx.fillStyle = T.magnet; ctx.fillRect(prof.l, yOf(S.magnet.high) - 1, prof.r - prof.l, yOf(S.magnet.low) - yOf(S.magnet.high) + 2); }
    }
    // candles: up = filled buy colour, down = hollow sell colour (shape is the second cue)
    const bodyW = Math.max(2, Math.floor(bw * 0.6));
    bars.forEach((b, i) => {
      const x = xOf(i), up = b.c >= b.o, col = up ? T.buy : T.sell;
      ctx.strokeStyle = col; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x, yOf(b.h)); ctx.lineTo(x, yOf(b.l)); ctx.stroke();
      const y0 = yOf(Math.max(b.o, b.c)), y1 = Math.max(y0 + 1, yOf(Math.min(b.o, b.c)));
      if (up) { ctx.fillStyle = col; ctx.fillRect(x - bodyW / 2, y0, bodyW, y1 - y0); }
      else { ctx.fillStyle = T.surface; ctx.fillRect(x - bodyW / 2, y0, bodyW, y1 - y0); ctx.lineWidth = 1.5; ctx.strokeRect(x - bodyW / 2, y0, bodyW, y1 - y0); }
    });
    // VWAP line
    if (vs.length === n) { ctx.strokeStyle = T.vwap; ctx.lineWidth = 2; ctx.beginPath(); vs.forEach((p, i) => i ? ctx.lineTo(xOf(i), yOf(p.vwap)) : ctx.moveTo(xOf(i), yOf(p.vwap))); ctx.stroke(); }
    // big trades: buy = filled disc, sell = ring; radius ~ sqrt(size); 2px surface ring separates overlaps
    const big = o.big || [];
    if (big.length) {
      const maxS = Math.max(...big.map(t => t.size)), idx = new Map(bars.map((b, i) => [b.t, i])), ivs = o.intervalMs || 60000;
      for (const t of big) {
        const i = idx.get(Math.floor(OF.bucketStart(t.ts, ivs) / 1000)); if (i == null) continue;
        const r = 4 + 9 * Math.sqrt(t.size / maxS), x = xOf(i), y = yOf(t.price);
        ctx.beginPath(); ctx.arc(x, y, r + 2, 0, Math.PI * 2); ctx.fillStyle = T.surface; ctx.fill();
        ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2);
        if (t.side === "buy") { ctx.fillStyle = T.buy; ctx.fill(); } else { ctx.strokeStyle = T.sell; ctx.lineWidth = 2; ctx.stroke(); }
      }
    }
    // profile strip: buy to the right of the midline, sell to the left (approximate profiles: total only, muted)
    if (S.profile) {
      const rows = S.profile.rows(), mid = (prof.l + prof.r) / 2, half = (prof.r - prof.l) / 2 - 2;
      const lvlH = Math.max(1, (plot.b - plot.t) * S.profile.tick / (hi - lo) - 1);
      const maxSide = Math.max(1e-12, ...rows.map(r => S.profile.approximate ? r.total : Math.max(r.buy, r.sell)));
      hline(ctx, mid, mid, plot.t, T.axis);
      ctx.strokeStyle = T.axis; ctx.beginPath(); ctx.moveTo(Math.round(mid) + 0.5, plot.t); ctx.lineTo(Math.round(mid) + 0.5, plot.b); ctx.stroke();
      for (const r of rows) {
        const y = yOf(r.price) - lvlH / 2;
        if (S.profile.approximate) { const len = half * 2 * r.total / maxSide; ctx.fillStyle = T.muted; ctx.fillRect(prof.l + 2, y, len, lvlH); }
        else { ctx.fillStyle = T.buy; ctx.fillRect(mid + 1, y, half * r.buy / maxSide, lvlH); ctx.fillStyle = T.sell; ctx.fillRect(mid - 1 - half * r.sell / maxSide, y, half * r.sell / maxSide, lvlH); }
      }
      if (S.range && S.range.poc != null) { hline(ctx, prof.l, prof.r, yOf(S.range.poc), T.ink, 1.5); ctx.fillStyle = T.ink2; ctx.textAlign = "left"; ctx.fillText("POC " + fmtP(S.range.poc, o.dec), prof.l + 2, yOf(S.range.poc) - 8); }
    }
    // walls strip: bars grow leftwards from the right edge; bid filled, ask hollow
    if (o.walls && o.walls.length) {
      const maxW = Math.max(...o.walls.map(x => x.size)), span = walls.r - walls.l - 4;
      ctx.font = "10px system-ui, -apple-system, 'Segoe UI', sans-serif";
      for (const wl of o.walls) {
        const len = Math.max(3, span * wl.size / maxW), y = yOf(wl.price), bh = 6;
        if (wl.side === "bid") { ctx.fillStyle = T.buy; ctx.fillRect(walls.r - len, y - bh / 2, len, bh); }
        else { ctx.strokeStyle = T.sell; ctx.lineWidth = 1.5; ctx.strokeRect(walls.r - len + 0.5, y - bh / 2 + 0.5, len - 1, bh - 1); }
        const label = fmtP(wl.price, o.dec) + " · " + fmtS(wl.size);
        ctx.fillStyle = T.ink2; ctx.textAlign = "right"; ctx.textBaseline = "middle";
        if (ctx.measureText(label).width < walls.r - len - walls.l - 4) ctx.fillText(label, walls.r - len - 4, y);
        else ctx.fillText(label, walls.r, y - bh / 2 - 7);  // bar fills the strip: label sits above it
      }
    }
    // last price line + tag
    if (S.last != null) {
      const y = yOf(S.last); hline(ctx, plot.l, prof.r, y, T.ink2, 1);
      const tag = fmtP(S.last, o.dec); ctx.font = "11px system-ui, -apple-system, 'Segoe UI', sans-serif";
      const tw = ctx.measureText(tag).width + 8; ctx.fillStyle = T.ink; ctx.fillRect(padL - tw - 2, y - 8, tw, 16);
      ctx.fillStyle = T.surface; ctx.textAlign = "right"; ctx.fillText(tag, padL - 6, y);
    }
    // x labels
    ctx.fillStyle = T.muted; ctx.textAlign = "center"; ctx.textBaseline = "alphabetic";
    const every = Math.max(1, Math.ceil(72 / bw));  // bars are capped at 14px, so space labels by pixels not by count
    bars.forEach((b, i) => { if ((n - 1 - i) % every === 0) ctx.fillText(fmtT(b.t), xOf(i), h - 6); });
    // hover crosshair
    if (o.hover && o.hover.i != null && bars[o.hover.i]) {
      const x = xOf(o.hover.i); ctx.strokeStyle = T.axis; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(x, plot.t); ctx.lineTo(x, plot.b); ctx.stroke();
      hline(ctx, plot.l, prof.r, o.hover.y, T.axis, 1, [3, 3]); ctx.setLineDash([]);
    }
    cv._layout = { plot, prof, walls, bw, n, xOf, yOf, lo, hi, bars };
  }

  /* CVD: single series, fill carries polarity (above zero buy colour, below sell colour). */
  function drawCvd(cv, series, bars, o) {
    const { ctx, w, h } = sizeCanvas(cv), T = tokens();
    if (!series || !series.length || !bars.length) return;
    const padL = 58, padT = 10, padB = 22, plot = { l: padL, r: w - 8, t: padT, b: h - padB };
    const idx = new Map(bars.map((b, i) => [b.t, i])), n = bars.length, bw = Math.min(14, (plot.r - plot.l) / n), xOf = i => plot.r - (n - 1 - i + 0.5) * bw;
    const vals = series.map(s => s[1]); let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals); const pad = (hi - lo || 1) * 0.1; lo -= pad; hi += pad;
    const yOf = v => plot.b - (v - lo) / (hi - lo) * (plot.b - plot.t), y0 = yOf(0);
    ctx.font = "11px system-ui, -apple-system, 'Segoe UI', sans-serif"; ctx.textBaseline = "middle";
    for (const v of niceTicks(lo, hi, 4)) { hline(ctx, plot.l, plot.r, yOf(v), T.grid); ctx.fillStyle = T.muted; ctx.textAlign = "right"; ctx.fillText(fmtS(v), padL - 6, yOf(v)); }
    const pts = series.map(([t, v]) => [xOf(idx.get(t) ?? 0), yOf(v)]);
    for (const [sign, col] of [[1, T.buy], [-1, T.sell]]) {
      ctx.save(); ctx.beginPath(); ctx.rect(plot.l, sign > 0 ? plot.t : y0, plot.r - plot.l, sign > 0 ? y0 - plot.t : plot.b - y0); ctx.clip();
      ctx.globalAlpha = 0.18; ctx.fillStyle = col; ctx.beginPath(); ctx.moveTo(pts[0][0], y0); pts.forEach(p => ctx.lineTo(p[0], p[1])); ctx.lineTo(pts[pts.length - 1][0], y0); ctx.closePath(); ctx.fill(); ctx.restore();
    }
    hline(ctx, plot.l, plot.r, y0, T.axis);
    ctx.strokeStyle = T.ink2; ctx.lineWidth = 2; ctx.beginPath(); pts.forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])); ctx.stroke();
    ctx.fillStyle = T.muted; ctx.textAlign = "center"; ctx.textBaseline = "alphabetic";
    const every = Math.max(1, Math.ceil(72 / bw));
    bars.forEach((b, i) => { if ((n - 1 - i) % every === 0) ctx.fillText(fmtT(b.t), xOf(i), h - 6); });
    if (o.hover && o.hover.i != null) { const x = xOf(o.hover.i); ctx.strokeStyle = T.axis; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(x, plot.t); ctx.lineTo(x, plot.b); ctx.stroke(); ctx.setLineDash([]); }
  }

  // ---- render ----------------------------------------------------------------
  let lastSummary = null;
  function render() {
    const src = currentSource(), st = stateFor(src);
    $("status").textContent = st.error ? `⚠ ข้อผิดพลาด: ${st.error} (แสดงข้อมูลล่าสุดที่มี)` :
      st.updated ? `● สด · อัปเดต ${new Date(st.updated).toLocaleTimeString("th-TH")} · เก็บมา ${st.trades.length.toLocaleString()} รายการ · order book ${st.books} ครั้ง` : "กำลังโหลด…";
    $("status").classList.toggle("err", !!st.error);
    $("proxyNote").hidden = !src.proxy;
    if (!st.trades.length) return;
    const cutoff = st.trades[st.trades.length - 1].ts - ui.windowMin * 60000;
    const wt = st.trades.filter(t => t.ts >= cutoff);
    const S = OF.summarizeTrades(wt, st.lastBook ? [st.lastBook] : [], src.tick, src.roundStep, intervalMs(), 0.70, 0.30, 0.99, 0, 4.0, BOOK_DEPTH, 60000, 1.0, st.tracker);
    if (S.error) return;
    S.walls.snapshots = st.books;
    lastSummary = S;
    const dec = src.dec, lb = st.lastBook;
    // stat tiles
    $("tLast").textContent = fmtP(S.last, dec);
    $("tBidAsk").textContent = lb ? `${fmtP(OF.bestBid(lb), dec)} / ${fmtP(OF.bestAsk(lb), dec)}` : "–";
    $("tSpread").textContent = lb ? "สเปรด " + fmtP(OF.bestAsk(lb) - OF.bestBid(lb), dec) : "";
    $("tCvd").textContent = fmtSigned(S.cvd.value); $("tCvd").className = "val " + (S.cvd.value >= 0 ? "up" : "down");
    $("tCvdSub").textContent = S.cvd.divergence ? TH.div[S.cvd.divergence] : `ซื้อ ${fmtS(S.profile_buy)} · ขาย ${fmtS(S.profile_sell)}`;
    const lastB = S.cvd.last_buckets[S.cvd.last_buckets.length - 1];
    $("tDelta").textContent = lastB ? fmtSigned(lastB.delta) : "–"; $("tDelta").className = "val " + (lastB && lastB.delta >= 0 ? "up" : "down");
    $("tDeltaSub").textContent = lastB ? `แท่ง ${ui.interval} ล่าสุด · ${lastB.n} รายการ` : "";
    $("tVwap").textContent = S.vwap ? fmtP(S.vwap.vwap, dec) : "–";
    $("tVwapSub").textContent = S.vwap ? `${TH.pos[S.vwap.position.label]} · z ${S.vwap.position.z.toFixed(2)} · ${TH.shape[S.vwap.shape] || "แถบ: ข้อมูลยังไม่พอ"}` : "";
    const byDist = (a, b) => a.dist - b.dist, nb = [...S.walls.bid].sort(byDist)[0], na = [...S.walls.ask].sort(byDist)[0];
    $("tWallBid").textContent = nb ? `${fmtP(nb.price, dec)} · ${fmtS(nb.size)}` : "–";
    $("tWallAsk").textContent = na ? `${fmtP(na.price, dec)} · ${fmtS(na.size)}` : "–";
    $("tRange").textContent = `${fmtP(S.range.low, dec)} – ${fmtP(S.range.high, dec)}`;
    $("tRangeSub").textContent = `POC ${fmtP(S.range.poc, dec)} · โซน 70% ${fmtP(S.range.va_low, dec)}–${fmtP(S.range.va_high, dec)} · แม่เหล็ก 30% ${fmtP(S.magnet.low, dec)}–${fmtP(S.magnet.high, dec)}`;
    // charts
    const walls = [...S.walls.bid, ...S.walls.ask];
    drawPriceChart($("cvMain"), S, { dec, walls, big: S.big_trades.list, intervalMs: intervalMs(), hover: ui.hover });
    drawCvd($("cvCvd"), S.cvd.series, S.bar_list, { hover: ui.hover });
    $("cvdNote").textContent = `CVD ${fmtSigned(S.cvd.value)} ในช่วง ${ui.windowMin} นาที` + (S.cvd.divergence ? ` · ${TH.div[S.cvd.divergence]}` : "");
    // tables
    const rowsW = walls.map(x => `<tr><td>${x.side === "bid" ? "BID (รับ)" : "ASK (ต้าน)"}</td><td class="num">${fmtP(x.price, dec)}</td><td class="num">${fmtS(x.size)}</td><td class="num">×${x.mult.toFixed(1)}</td><td class="num">${x.dist_pct.toFixed(2)}%</td><td>${TH.status[x.status]}${x.persistent ? " · ยืนนาน" : ""}</td><td class="num">${x.seen}</td><td class="num">${fmtAge(x.age_ms)}</td><td class="num">${(x.growth * 100).toFixed(0)}%</td></tr>`).join("");
    $("tblWalls").innerHTML = rowsW || `<tr><td colspan="9" class="empty">ยังไม่พบกำแพงที่ใหญ่กว่า 4 เท่าของมัธยฐานในระยะ 1% จากราคา</td></tr>`;
    $("tblEvents").innerHTML = S.walls.events.slice().reverse().map(e => `<tr><td>${e.side === "bid" ? "BID" : "ASK"}</td><td class="num">${fmtP(e.price, dec)}</td><td class="num">${fmtS(e.size)}</td><td>${TH.status[e.outcome]}</td><td class="num">${fmtAge(e.age_ms)}</td><td class="num">${new Date(e.ended_ts).toLocaleTimeString("th-TH")}</td></tr>`).join("")
      || `<tr><td colspan="6" class="empty">ยังไม่มีกำแพงที่หายไป (ต้องเห็นอย่างน้อย 2 ครั้งก่อน)</td></tr>`;
    $("bigNote").textContent = `เกณฑ์ไม้ใหญ่ = P99 ของขนาดในช่วงนี้ ≈ ${fmtS(S.big_trades.threshold)} · พบ ${S.big_trades.count} รายการ`;
    $("tblBig").innerHTML = S.big_trades.zones.map(z => `<tr><td class="num">${fmtP(z.price, dec)}</td><td class="num">${fmtS(z.buy)}</td><td class="num">${fmtS(z.sell)}</td><td class="num">${fmtSigned(z.delta)}</td><td class="num">${z.n}</td><td>${z.contested ? "⚔ ปะทะสองฝั่ง" : ""}</td></tr>`).join("")
      || `<tr><td colspan="6" class="empty">ยังไม่มี</td></tr>`;
    $("tblDelta").innerHTML = S.cvd.buckets.slice(-12).reverse().map(b => `<tr><td>${fmtT(b.t)}</td><td class="num">${fmtS(b.buy)}</td><td class="num">${fmtS(b.sell)}</td><td class="num ${b.delta >= 0 ? "up" : "down"}">${fmtSigned(b.delta)}</td><td class="num">${b.n}</td></tr>`).join("");
  }

  // ---- hover ----------------------------------------------------------------
  function onHover(ev) {
    const cv = $("cvMain"), L = cv._layout; if (!L || !lastSummary) return;
    const rect = cv.getBoundingClientRect(), x = ev.clientX - rect.left, y = ev.clientY - rect.top;
    const i = Math.max(0, Math.min(L.n - 1, L.n - 1 - Math.floor((L.plot.r - x) / L.bw)));
    ui.hover = (x >= L.plot.l && x <= L.walls.r && y >= L.plot.t && y <= L.plot.b) ? { i, y } : null;
    const tip = $("tip");
    if (!ui.hover) { tip.hidden = true; render(); return; }
    const src = currentSource(), b = L.bars[i], bk = lastSummary.cvd.buckets.find(q => q.t === b.t);
    const price = L.lo + (L.plot.b - y) / (L.plot.b - L.plot.t) * (L.hi - L.lo);
    let html = `<b>${fmtT(b.t)}</b> · ราคาที่เมาส์ ${fmtP(price, src.dec)}<br>O ${fmtP(b.o, src.dec)} H ${fmtP(b.h, src.dec)} L ${fmtP(b.l, src.dec)} C ${fmtP(b.c, src.dec)}<br>ปริมาณ ${fmtS(b.v)}` + (bk ? ` · delta ${fmtSigned(bk.delta)} (${bk.n} รายการ)` : "");
    if (x >= L.walls.l) { const near = [...lastSummary.walls.bid, ...lastSummary.walls.ask].sort((a, c) => Math.abs(a.price - price) - Math.abs(c.price - price))[0]; if (near) html += `<br>กำแพง ${near.side.toUpperCase()} ${fmtP(near.price, src.dec)} · ${fmtS(near.size)} · ${TH.status[near.status]} · ${fmtAge(near.age_ms)}`; }
    tip.innerHTML = html; tip.hidden = false;
    tip.style.left = Math.min(rect.width - tip.offsetWidth - 8, x + 14) + "px"; tip.style.top = Math.max(0, y - tip.offsetHeight - 10) + "px";
    render();
  }

  // ---- wiring ----------------------------------------------------------------
  function applyTheme(t) { if (t) document.documentElement.setAttribute("data-theme", t); else document.documentElement.removeAttribute("data-theme"); store.set("theme", t || ""); }
  function init() {
    const selS = $("selSource"); SOURCES.forEach(s => { const o = document.createElement("option"); o.value = s.key; o.textContent = s.label; selS.appendChild(o); });
    selS.value = ui.source; $("selWindow").value = String(ui.windowMin); $("selInterval").value = ui.interval;
    selS.addEventListener("change", () => { ui.source = selS.value; store.set("source", ui.source); render(); poll(); });
    $("selWindow").addEventListener("change", e => { ui.windowMin = +e.target.value; store.set("window", ui.windowMin); render(); });
    $("selInterval").addEventListener("change", e => { ui.interval = e.target.value; store.set("interval", ui.interval); render(); });
    const saved = store.get("theme", ""); if (saved) applyTheme(saved);
    $("btnTheme").addEventListener("click", () => { const dark = document.documentElement.getAttribute("data-theme") === "dark" || (!document.documentElement.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches); applyTheme(dark ? "light" : "dark"); render(); });
    const cv = $("cvMain"); cv.addEventListener("mousemove", onHover); cv.addEventListener("mouseleave", () => { ui.hover = null; $("tip").hidden = true; render(); });
    let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(render, 120); });
    document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
    poll(); setInterval(poll, POLL_MS);
  }
  document.addEventListener("DOMContentLoaded", init);
})();
