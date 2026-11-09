/*
 * Order-flow screener: Binance public market data -> analytics (engine.js) -> canvas/DOM.
 * Everything runs in the browser; no API key and no backend.
 */
(() => {
  'use strict';
  const E = window.OFEngine;

  // ---------------------------------------------------------------- config
  const PRESETS = {
    futures: [
      ['XAUUSDT', 'Gold (XAU) perp'],
      ['XAGUSDT', 'Silver (XAG) perp'],
      ['BTCUSDT', 'BTC perp'],
      ['ETHUSDT', 'ETH perp'],
      ['SOLUSDT', 'SOL perp'],
    ],
    spot: [
      ['PAXGUSDT', 'PAX Gold (PAXG)'],
      ['BTCUSDT', 'BTC'],
      ['ETHUSDT', 'ETH'],
      ['SOLUSDT', 'SOL'],
    ],
  };
  // USDⓈ-M futures streams are split by path since 2026: depth on /public, trades on /market.
  const ENDPOINTS = {
    futures: {
      rest: ['https://fapi.binance.com/fapi/v1/klines'],
      klineLimit: 1500,
      ws: (s) => [
        `wss://fstream.binance.com/market/stream?streams=${s}@aggTrade`,
        `wss://fstream.binance.com/public/stream?streams=${s}@depth20@100ms`,
      ],
    },
    spot: {
      rest: ['https://api.binance.com/api/v3/klines', 'https://data-api.binance.vision/api/v3/klines'],
      klineLimit: 1000,
      ws: (s) => [`wss://stream.binance.com:9443/stream?streams=${s}@aggTrade/${s}@depth20@100ms`],
    },
  };
  const TFS = [[1, '1s'], [5, '5s'], [15, '15s'], [60, '1m'], [300, '5m'], [900, '15m']];
  const Z_TFS = [[1, '1s bars'], [5, '5s bars'], [15, '15s bars'], [60, '1m bars'], [300, '5m bars']];
  const MAX_SECOND_BARS = 4 * 3600;
  const MAX_MINUTE_BARS = 3000;
  const MAX_TRADES = 3000;
  const SIZE_WINDOW = 500;
  const RV_WINDOW = 30; // 1-minute returns
  const HISTORY_BEFORE_SESSION = 12 * 3600;
  const STORE_KEY = 'orderflow.settings.v1';

  const $ = (id) => document.getElementById(id);
  const rootStyle = getComputedStyle(document.documentElement);
  const cssVar = (n) => rootStyle.getPropertyValue(n).trim();
  const COL = {
    text: cssVar('--text'),
    muted: cssVar('--muted'),
    up: cssVar('--up'),
    down: cssVar('--down'),
    gold: cssVar('--gold'),
    vwap: cssVar('--vwap'),
    cvd: cssVar('--cvd'),
    warn: cssVar('--warn'),
    grid: '#1a222d',
    axis: '#2a3442',
  };

  // ---------------------------------------------------------------- settings
  function loadSettings() {
    try {
      return JSON.parse(localStorage.getItem(STORE_KEY)) || {};
    } catch (e) {
      return {};
    }
  }
  function saveSettings() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(cfg));
    } catch (e) {
      /* storage unavailable: settings just won't persist */
    }
  }
  function cleanSymbol(s) {
    s = String(s || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
    return s.length >= 5 && s.length <= 20 ? s : '';
  }
  const isMarket = (v) => v === 'futures' || v === 'spot';
  const pick = (v, ok, d) => (ok(v) ? v : d);

  const qs = new URLSearchParams(location.search);
  const saved = loadSettings();
  const cfg = {
    market: pick(qs.get('market') || saved.market, isMarket, 'futures'),
    symbol: '',
    tf: pick(+saved.tf, (v) => TFS.some((t) => t[0] === v), 60),
    zTf: pick(+saved.zTf, (v) => Z_TFS.some((t) => t[0] === v), 5),
    zN: pick(+saved.zN, (v) => [20, 50, 100].includes(v), 20),
    bigMult: pick(+saved.bigMult, (v) => [1, 2, 5, 10].includes(v), 5),
    anchor: pick(saved.anchor, (v) => v === 'utc' || v === 'ny', 'utc'),
  };
  cfg.symbol = cleanSymbol(qs.get('symbol') || saved.symbol) || PRESETS[cfg.market][0][0];

  // ---------------------------------------------------------------- state
  let gen = 0;
  let S = null;
  let sockets = [];

  function newState() {
    return {
      gen: ++gen,
      minute: [], // 1m bars: seeded from klines, then live
      second: [], // 1s bars: live only
      trades: [],
      tradeVer: 0,
      sizes: [],
      sizeSum: 0,
      tradeTimes: [],
      book: { bids: [], asks: [] },
      obi5: NaN,
      obi20: NaN,
      obiEma: NaN,
      obiHist: [],
      lastObiSample: 0,
      signals: [],
      sigVer: 0,
      sess: null,
      last: NaN,
      step: 0,
      pd: 2,
      skew: 0,
      skewSet: false,
      expected: 0,
      open: 0,
      connectedAt: 0,
      startedAt: 0,
      lastMsg: 0,
      seedError: false,
      noDataWarned: false,
      zBarT: null,
      divBarT: null,
      divState: null,
      regimeBarT: null,
      regime: null,
      lastBigSig: 0,
      lastBigMult: 0,
      rendered: { tape: -1, tapeMult: 0, sig: -1 },
    };
  }

  // ---------------------------------------------------------------- sessions
  function tzOffsetMin(tsSec, tz) {
    const part = new Intl.DateTimeFormat('en-US', { timeZone: tz, timeZoneName: 'shortOffset' })
      .formatToParts(new Date(tsSec * 1000))
      .find((p) => p.type === 'timeZoneName');
    const m = part && part.value.match(/GMT([+-])(\d{1,2})(?::(\d{2}))?/);
    if (!m) return 0;
    return (m[1] === '-' ? -1 : 1) * (+m[2] * 60 + +(m[3] || 0));
  }
  // Most recent 17:00 New York time at or before nowSec (gold / FX trading day).
  function nyCloseBefore(nowSec) {
    for (let back = 0; back < 3; back++) {
      const ymd = new Intl.DateTimeFormat('en-CA', {
        timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
      }).format(new Date((nowSec - back * 86400) * 1000));
      const [y, mo, d] = ymd.split('-').map(Number);
      const naive = Date.UTC(y, mo - 1, d, 17) / 1000;
      let t = naive - tzOffsetMin(naive, 'America/New_York') * 60;
      t = naive - tzOffsetMin(t, 'America/New_York') * 60;
      if (t <= nowSec) return t;
    }
    return null;
  }
  function sessionStartFor(nowSec, anchor) {
    if (anchor === 'ny') {
      try {
        const t = nyCloseBefore(nowSec);
        if (t !== null) return t;
      } catch (e) {
        /* Intl without shortOffset: fall back to UTC */
      }
    }
    return Math.floor(nowSec / 86400) * 86400;
  }
  function freshSession(start) {
    return {
      start,
      end: sessionStartFor(start + 26 * 3600, cfg.anchor),
      open: NaN,
      high: -Infinity,
      low: Infinity,
      vol: 0,
      delta: 0,
      seededDelta: 0,
      vwap: E.createVwap(),
      profile: new Map(),
    };
  }
  function rollSession(sec) {
    S.sess = freshSession(sessionStartFor(sec, cfg.anchor));
    signal('info', 'Session ใหม่', `เริ่ม ${fmtClock(S.sess.start * 1000, false)}`);
  }

  // ---------------------------------------------------------------- data in
  function setPriceScale(p) {
    if (S.step || !(p > 0)) return;
    S.step = E.niceStep(p * 2e-4);
    S.pd = p >= 100 ? 2 : p >= 10 ? 3 : p >= 1 ? 4 : 6;
  }
  function profileAdd(price, buy, sell) {
    if (!S.step) return;
    const k = Math.round(price / S.step);
    const r = S.sess.profile.get(k);
    if (r) {
      r.buy += buy;
      r.sell += sell;
    } else S.sess.profile.set(k, { buy, sell });
  }

  async function fetchJson(bases, query) {
    let lastErr;
    for (const base of bases) {
      try {
        const r = await fetch(base + query);
        const j = await r.json().catch(() => null);
        if (!r.ok) throw new Error((j && j.msg) || `HTTP ${r.status}`);
        return j;
      } catch (e) {
        lastErr = e;
      }
    }
    throw lastErr;
  }
  async function fetchKlines(market, symbol, startSec, endSec) {
    const ep = ENDPOINTS[market];
    const out = [];
    let from = startSec * 1000;
    for (let guard = 0; guard < 10 && from < endSec * 1000; guard++) {
      const rows = await fetchJson(ep.rest, `?symbol=${symbol}&interval=1m&startTime=${from}&limit=${ep.klineLimit}`);
      if (!Array.isArray(rows)) throw new Error((rows && rows.msg) || 'unexpected response');
      if (!rows.length) break;
      out.push(...rows);
      from = rows[rows.length - 1][0] + 60000;
      if (rows.length < ep.klineLimit) break;
    }
    return out;
  }

  // History from closed 1m klines. The still-open minute is dropped; live trades rebuild it.
  function applySeed(rows) {
    const nowMin = Math.floor(Date.now() / 60000) * 60;
    const bars = E.parseKlines(rows).filter((b) => b.t < nowMin);
    if (!bars.length) return;
    setPriceScale(bars[bars.length - 1].c);
    const s = S.sess;
    for (const b of bars) {
      b.seeded = true;
      if (b.t < s.start) continue;
      const vol = b.buy + b.sell;
      const d = b.buy - b.sell;
      if (!Number.isFinite(s.open)) s.open = b.o;
      s.high = Math.max(s.high, b.h);
      s.low = Math.min(s.low, b.l);
      s.vol += vol;
      s.delta += d;
      s.seededDelta += d;
      const tp = (b.h + b.l + b.c) / 3;
      E.vwapAdd(s.vwap, tp, vol);
      profileAdd(tp, b.buy, b.sell);
    }
    S.minute = bars.slice(-MAX_MINUTE_BARS);
    S.last = bars[bars.length - 1].c;
  }

  function upsertBar(arr, t, p, buy, sell, cap) {
    let b = arr[arr.length - 1];
    if (!b || t > b.t) {
      b = { t, o: p, h: p, l: p, c: p, buy: 0, sell: 0, n: 0 };
      arr.push(b);
      if (arr.length > cap) arr.splice(0, arr.length - cap);
    }
    if (p > b.h) b.h = p;
    if (p < b.l) b.l = p;
    b.c = p;
    b.buy += buy;
    b.sell += sell;
    b.n++;
  }

  function onTrade(d) {
    const p = +d.p;
    const q = +d.q;
    const T = +d.T;
    if (!(p > 0) || !(q > 0) || !(T > 0)) return;
    const now = Date.now();
    const sk = T - now;
    S.skew = S.skewSet ? S.skew * 0.95 + sk * 0.05 : sk;
    S.skewSet = true;

    const sellAgg = !!d.m; // buyer is the maker -> the seller hit the bid
    const buy = sellAgg ? 0 : q;
    const sell = sellAgg ? q : 0;
    const sec = Math.floor(T / 1000);
    setPriceScale(p);
    if (sec >= S.sess.end) rollSession(sec);
    S.last = p;

    upsertBar(S.second, sec, p, buy, sell, MAX_SECOND_BARS);
    upsertBar(S.minute, Math.floor(sec / 60) * 60, p, buy, sell, MAX_MINUTE_BARS);

    const s = S.sess;
    if (!Number.isFinite(s.open)) s.open = p;
    if (p > s.high) s.high = p;
    if (p < s.low) s.low = p;
    s.vol += q;
    s.delta += buy - sell;
    E.vwapAdd(s.vwap, p, q);
    profileAdd(p, buy, sell);

    const avg = S.sizes.length >= 50 ? S.sizeSum / S.sizes.length : NaN;
    const mult = Number.isFinite(avg) && avg > 0 ? q / avg : NaN;
    S.sizes.push(q);
    S.sizeSum += q;
    if (S.sizes.length > SIZE_WINDOW) S.sizeSum -= S.sizes.shift();
    S.trades.push({ T, p, q, sellAgg, mult });
    if (S.trades.length > MAX_TRADES) S.trades.splice(0, S.trades.length - MAX_TRADES);
    S.tradeVer++;
    S.tradeTimes.push(now);
    while (S.tradeTimes.length && S.tradeTimes[0] < now - 10000) S.tradeTimes.shift();

    if (mult >= 10 && (now - S.lastBigSig > 3000 || mult >= 2 * S.lastBigMult)) {
      S.lastBigSig = now;
      S.lastBigMult = mult;
      signal(sellAgg ? 'down' : 'up', `เทรดใหญ่ ${sellAgg ? 'SELL' : 'BUY'} ${fmtQty(q)}`,
        `${mult.toFixed(0)}× ค่าเฉลี่ย @ ${fmtPrice(p)}`);
    }
  }

  function parseLevels(levels) {
    const out = [];
    for (const l of levels) {
      const p = +l[0];
      const q = +l[1];
      if (p > 0 && q > 0) out.push([p, q]);
    }
    return out;
  }
  function onDepth(bids, asks) {
    S.book.bids = parseLevels(bids).sort((x, y) => y[0] - x[0]);
    S.book.asks = parseLevels(asks).sort((x, y) => x[0] - y[0]);
    if (S.book.bids.length) setPriceScale(S.book.bids[0][0]);
    S.obi5 = E.bookImbalance(S.book.bids, S.book.asks, 5);
    S.obi20 = E.bookImbalance(S.book.bids, S.book.asks, 20);
    if (Number.isFinite(S.obi20)) {
      S.obiEma = Number.isFinite(S.obiEma) ? S.obiEma + 0.1 * (S.obi20 - S.obiEma) : S.obi20;
    }
    const now = Date.now();
    if (now - S.lastObiSample >= 1000 && Number.isFinite(S.obiEma)) {
      S.lastObiSample = now;
      S.obiHist.push(S.obiEma);
      if (S.obiHist.length > 300) S.obiHist.shift();
    }
  }

  // ---------------------------------------------------------------- sockets
  function connect(g) {
    const urls = ENDPOINTS[cfg.market].ws(cfg.symbol.toLowerCase());
    S.expected = urls.length;
    S.startedAt = Date.now();
    urls.forEach((u) => openSocket(u, g, 0));
  }
  function openSocket(url, g, attempt) {
    if (g !== gen) return;
    let ws;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      showBanner(`เปิด WebSocket ไม่ได้: ${e.message}`);
      return;
    }
    sockets.push(ws);
    let opened = false;
    ws.onopen = () => {
      if (g !== gen) return;
      opened = true;
      attempt = 0;
      S.open++;
      if (!S.connectedAt) S.connectedAt = Date.now();
    };
    ws.onmessage = (ev) => {
      if (g !== gen) return;
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      const d = msg && msg.data ? msg.data : msg;
      if (!d || typeof d !== 'object') return;
      S.lastMsg = Date.now();
      if (d.e === 'aggTrade') onTrade(d);
      else if (Array.isArray(d.b) && Array.isArray(d.a)) onDepth(d.b, d.a); // futures partial depth
      else if (Array.isArray(d.bids) && Array.isArray(d.asks)) onDepth(d.bids, d.asks); // spot partial depth
    };
    ws.onclose = () => {
      sockets = sockets.filter((x) => x !== ws);
      if (g !== gen) return;
      if (opened) S.open = Math.max(0, S.open - 1);
      const delay = Math.min(30000, 1000 * 2 ** attempt);
      setTimeout(() => openSocket(url, g, attempt + 1), delay);
    };
  }
  function closeSockets() {
    for (const ws of sockets) {
      ws.onclose = null;
      ws.onmessage = null;
      try {
        ws.close();
      } catch (e) {
        /* already closed */
      }
    }
    sockets = [];
  }

  async function start() {
    closeSockets();
    S = newState();
    const g = S.gen;
    syncControls();
    saveSettings();
    try {
      history.replaceState(null, '', `?market=${cfg.market}&symbol=${cfg.symbol}`);
    } catch (e) {
      /* sandboxed frame */
    }
    hideBanner();
    const nowSec = Math.floor(Date.now() / 1000);
    S.sess = freshSession(sessionStartFor(nowSec, cfg.anchor));
    render();
    try {
      const rows = await fetchKlines(cfg.market, cfg.symbol, S.sess.start - HISTORY_BEFORE_SESSION, nowSec);
      if (g !== gen) return;
      applySeed(rows);
    } catch (e) {
      if (g !== gen) return;
      S.seedError = true;
      showBanner(explainError(e));
    }
    if (g !== gen) return;
    connect(g);
    render();
  }

  function explainError(e) {
    const msg = String((e && e.message) || e);
    const where = cfg.market === 'futures' ? 'Futures (Perp)' : 'Spot';
    if (/invalid symbol/i.test(msg)) return `ไม่พบสัญลักษณ์ ${cfg.symbol} ในตลาด Binance ${where}`;
    if (/restricted location|eligibility/i.test(msg)) {
      return 'Binance ไม่ให้บริการข้อมูลจากตำแหน่ง/เครือข่ายนี้ (restricted location) — ลองเครือข่ายอื่น';
    }
    return `โหลดประวัติจาก Binance ไม่สำเร็จ (${msg}) — กราฟจะเริ่มจากข้อมูล live`;
  }

  // ---------------------------------------------------------------- signals
  function signal(side, title, detail) {
    S.signals.unshift({ t: Date.now(), side, title, detail });
    if (S.signals.length > 60) S.signals.pop();
    S.sigVer++;
  }

  // ---------------------------------------------------------------- formatting
  const fmtPrice = (p) =>
    Number.isFinite(p) ? p.toLocaleString('en-US', { minimumFractionDigits: S.pd, maximumFractionDigits: S.pd }) : '–';
  function fmtQty(q) {
    if (!Number.isFinite(q)) return '–';
    const a = Math.abs(q);
    if (a >= 1e9) return (q / 1e9).toFixed(2) + 'B';
    if (a >= 1e6) return (q / 1e6).toFixed(2) + 'M';
    if (a >= 1e4) return (q / 1e3).toFixed(1) + 'K';
    if (a >= 100) return q.toFixed(0);
    if (a >= 10) return q.toFixed(1);
    if (a >= 1) return q.toFixed(2);
    return q.toFixed(a >= 0.01 || a === 0 ? 3 : 5);
  }
  const fmtSigned = (q) => (!Number.isFinite(q) ? '–' : (q > 0 ? '+' : q < 0 ? '−' : '') + fmtQty(Math.abs(q)));
  const fmtPct = (v) => (Number.isFinite(v) ? (v * 100).toFixed(1) + '%' : '–');
  const fmtNum = (v, d) => (Number.isFinite(v) ? (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v).toFixed(d) : '–');
  const pad2 = (n) => String(n).padStart(2, '0');
  function fmtClock(ms, withSec = true) {
    const d = new Date(ms);
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}${withSec ? ':' + pad2(d.getSeconds()) : ''}`;
  }
  const signCls = (v) => (v > 0 ? 'pos' : v < 0 ? 'neg' : '');
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  const baseAsset = () => cfg.symbol.replace(/(USDT|USDC|FDUSD|BUSD|USD)$/, '') || cfg.symbol;
  const tfLabel = (tf) => (TFS.find((t) => t[0] === tf) || [0, `${tf}s`])[1];

  // ---------------------------------------------------------------- canvas helpers
  function prep(canvas) {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (!w || !h) return null;
    const W = Math.round(w * dpr);
    const H = Math.round(h * dpr);
    if (canvas.width !== W || canvas.height !== H) {
      canvas.width = W;
      canvas.height = H;
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
    ctx.textBaseline = 'middle';
    ctx.lineWidth = 1;
    return { ctx, w, h };
  }
  function emptyMsg(g, text) {
    g.ctx.fillStyle = COL.muted;
    g.ctx.textAlign = 'center';
    g.ctx.font = '12px -apple-system, "Segoe UI", "Noto Sans Thai", sans-serif';
    g.ctx.fillText(text, g.w / 2, g.h / 2);
  }
  function hline(ctx, x1, x2, y, color, dash) {
    ctx.save();
    ctx.strokeStyle = color;
    if (dash) ctx.setLineDash(dash);
    ctx.beginPath();
    ctx.moveTo(x1, Math.round(y) + 0.5);
    ctx.lineTo(x2, Math.round(y) + 0.5);
    ctx.stroke();
    ctx.restore();
  }
  function vline(ctx, x, y1, y2, color, dash) {
    ctx.save();
    ctx.strokeStyle = color;
    if (dash) ctx.setLineDash(dash);
    ctx.beginPath();
    ctx.moveTo(Math.round(x) + 0.5, y1);
    ctx.lineTo(Math.round(x) + 0.5, y2);
    ctx.stroke();
    ctx.restore();
  }
  function tag(ctx, x, y, text, bg, fg, minW = 0) {
    const w = Math.max(minW, ctx.measureText(text).width + 8);
    ctx.fillStyle = bg;
    ctx.fillRect(x, y - 8, w, 16);
    ctx.fillStyle = fg;
    ctx.textAlign = 'left';
    ctx.fillText(text, x + 4, y);
  }
  // Right-axis labels: lines at the true price, tags nudged apart so they never overlap.
  function axisTags(ctx, x, marks, top, bot, lineX1, lineX2, minW) {
    for (const mk of marks) {
      mk.y = Math.min(bot - 8, Math.max(top + 8, mk.y));
      if (mk.line) hline(ctx, lineX1, lineX2, mk.y, mk.line, mk.dash);
    }
    const sorted = marks.slice().sort((a, b) => a.y - b.y);
    for (let i = 1; i < sorted.length; i++) {
      if (sorted[i].y < sorted[i - 1].y + 16) sorted[i].y = sorted[i - 1].y + 16;
    }
    const over = sorted.length ? sorted[sorted.length - 1].y - (bot - 8) : 0;
    if (over > 0) sorted.forEach((mk) => (mk.y -= over));
    for (const mk of sorted) tag(ctx, x, mk.y, mk.label, mk.bg, mk.fg, minW);
  }
  function timeLabels(ctx, view, xAt, tf, y, minGapPx, bw) {
    const every = Math.max(1, Math.ceil(minGapPx / bw));
    ctx.fillStyle = COL.muted;
    ctx.textAlign = 'center';
    view.forEach((b, i) => {
      if (Math.floor(b.t / tf) % every !== 0) return;
      ctx.fillText(fmtClock(b.t * 1000, tf < 60), xAt(i), y);
    });
  }

  // ---------------------------------------------------------------- analytics per frame
  function barsFor(tf, nowSec) {
    const src = tf >= 60 ? S.minute : S.second;
    if (!src.length) return [];
    return E.resample(src, tf, Math.floor(nowSec / tf) * tf);
  }

  function computeZ(bars, nowSec) {
    const vols = bars.map((b) => b.buy + b.sell);
    const z = E.zScores(vols, cfg.zN);
    let ci = bars.length - 1;
    while (ci >= 0 && bars[ci].t + cfg.zTf > nowSec) ci--;
    const cur = ci >= 0 ? z[ci] : null;
    if (ci >= 0 && bars[ci].t !== S.zBarT) {
      if (S.zBarT !== null && cur !== null && cur >= 3) {
        const b = bars[ci];
        const d = b.buy - b.sell;
        signal(d >= 0 ? 'up' : 'down', `Volume spike z ${cur.toFixed(1)}`,
          `${tfLabel(cfg.zTf)} · vol ${fmtQty(vols[ci])} · Δ ${fmtSigned(d)} @ ${fmtPrice(b.c)}`);
      }
      S.zBarT = bars[ci].t;
    }
    return { z, ci, cur, vols };
  }

  function computeVol(minBars, chartBars) {
    const closes = minBars.map((b) => b.c);
    const rv = E.rollingRealizedVol(closes, RV_WINDOW, 60, 365);
    const sample = rv.filter((v) => v !== null);
    const cur = rv.length ? rv[rv.length - 1] : null;
    const pct = cur === null ? NaN : E.percentileRank(sample, cur);
    const regime = sample.length >= 60 ? E.volRegime(pct) : 'n/a';
    const last = minBars.slice(-RV_WINDOW);
    const pk = E.parkinsonVol(last.map((b) => b.h), last.map((b) => b.l), 60, 365);
    const atr1 = E.atr(minBars.slice(-300), 14);
    const atrTf = E.atr(chartBars.slice(-300), 14);
    const lb = chartBars[chartBars.length - 1];
    const rng = lb && atrTf > 0 ? (lb.h - lb.l) / atrTf : NaN;
    const q = [0.2, 0.7, 0.9].map((x) => E.quantile(sample, x));

    const lastT = minBars.length ? minBars[minBars.length - 1].t : null;
    if (lastT !== null && lastT !== S.regimeBarT && regime !== 'n/a') {
      if (S.regime && regime !== S.regime) {
        signal('info', `Volatility ${S.regime} → ${regime}`, `RV ${fmtPct(cur)} · P${pct.toFixed(0)}`);
      }
      S.regime = regime;
      S.regimeBarT = lastT;
    }
    return { rv, cur, pct, regime, pk, atr1, atrTf, rng, q, bars: minBars };
  }

  function profileRows() {
    const prof = S.sess.profile;
    if (!prof.size || !S.step) return [];
    const keys = [...prof.keys()].sort((a, b) => a - b);
    const rows = [];
    if (keys[keys.length - 1] - keys[0] <= 5000) {
      for (let k = keys[0]; k <= keys[keys.length - 1]; k++) {
        const r = prof.get(k);
        rows.push({ k, buy: r ? r.buy : 0, sell: r ? r.sell : 0 });
      }
    } else {
      for (const k of keys) rows.push({ k, ...prof.get(k) });
    }
    return rows;
  }

  // ---------------------------------------------------------------- drawing
  function drawChart(bars, nowSec, prof) {
    const g = prep($('c-chart'));
    const flag = $('div-flag');
    if (!g) return;
    const { ctx, w, h } = g;
    if (!bars.length) {
      flag.className = 'flag';
      return emptyMsg(g, cfg.tf < 60 ? 'รอข้อมูล live สำหรับ timeframe วินาที…' : 'กำลังโหลดข้อมูล…');
    }

    // CVD over every bar, reset at the session start.
    const cvd = new Array(bars.length);
    let acc = 0;
    let sessIdx = -1;
    for (let i = 0; i < bars.length; i++) {
      if (sessIdx < 0 && bars[i].t >= S.sess.start) {
        sessIdx = i;
        acc = cfg.tf < 60 ? S.sess.seededDelta : 0;
      }
      acc += bars[i].buy - bars[i].sell;
      cvd[i] = acc;
    }

    // Divergence on completed bars, evaluated once per new bar.
    const formingT = bars[bars.length - 1].t;
    if (formingT !== S.divBarT) {
      const done = bars.length - 1;
      const st = E.cvdDivergence(bars.slice(0, done).map((b) => b.c), cvd.slice(0, done), 30);
      if (S.divBarT !== null && st && st !== S.divState) {
        signal(st === 'bearish' ? 'down' : 'up', `CVD divergence (${st})`,
          `${tfLabel(cfg.tf)} · ราคา ${st === 'bearish' ? 'ทำ high ใหม่แต่ CVD ไม่ตาม' : 'ทำ low ใหม่แต่ CVD ไม่ตาม'}`);
      }
      S.divState = st;
      S.divBarT = formingT;
    }
    flag.className = S.divState === 'bearish' ? 'flag bear' : S.divState === 'bullish' ? 'flag bull' : 'flag';
    flag.textContent = S.divState ? `${S.divState} divergence` : '';

    const padL = 6;
    const padR = 74;
    const padT = 8;
    const padB = 18;
    const plotW = w - padL - padR;
    const slots = Math.max(20, Math.floor(plotW / 6));
    const i0 = Math.max(0, bars.length - slots);
    const view = bars.slice(i0);
    const cv = cvd.slice(i0);
    const bw = plotW / slots;
    const xAt = (i) => padL + (slots - view.length + i + 0.5) * bw;
    const pH = Math.round((h - padT - padB) * 0.63);
    const cTop = padT + pH + 12;
    const cH = h - padB - cTop;

    // price scale
    let lo = Infinity;
    let hi = -Infinity;
    for (const b of view) {
      if (b.l < lo) lo = b.l;
      if (b.h > hi) hi = b.h;
    }
    const { vwap, sd } = E.vwapValue(S.sess.vwap);
    const span0 = hi - lo || hi * 1e-4 || 1;
    for (const v of [vwap, vwap + sd, vwap - sd, prof && prof.poc]) {
      if (Number.isFinite(v) && v > lo - span0 * 0.35 && v < hi + span0 * 0.35) {
        lo = Math.min(lo, v);
        hi = Math.max(hi, v);
      }
    }
    const padP = (hi - lo) * 0.06 || hi * 1e-4 || 1;
    lo -= padP;
    hi += padP;
    const yP = (p) => padT + ((hi - p) / (hi - lo)) * pH;
    const inP = (p) => Number.isFinite(p) && p >= lo && p <= hi;

    const step = E.niceStep((hi - lo) / 5);
    ctx.textAlign = 'left';
    for (let k = Math.ceil(lo / step); k * step <= hi; k++) {
      const y = yP(k * step);
      hline(ctx, padL, w - padR, y, COL.grid);
      ctx.fillStyle = COL.muted;
      ctx.fillText(fmtPrice(k * step), w - padR + 6, y);
    }

    // session boundary
    const sIdx = sessIdx - i0;
    if (sIdx > 0 && sIdx < view.length) {
      const x = xAt(sIdx) - bw / 2;
      vline(ctx, x, padT, h - padB, COL.axis, [4, 4]);
      ctx.fillStyle = COL.muted;
      ctx.textAlign = 'left';
      ctx.fillText('session', x + 4, padT + 6);
    }

    // VWAP ±1σ and POC
    if (inP(vwap + sd)) hline(ctx, padL, w - padR, yP(vwap + sd), 'rgba(90,168,255,.45)', [3, 4]);
    if (inP(vwap - sd)) hline(ctx, padL, w - padR, yP(vwap - sd), 'rgba(90,168,255,.45)', [3, 4]);
    if (inP(vwap)) hline(ctx, padL, w - padR, yP(vwap), COL.vwap);
    if (prof && inP(prof.poc)) hline(ctx, padL, w - padR, yP(prof.poc), COL.gold, [6, 3]);

    // candles
    const body = Math.max(1, Math.min(10, bw * 0.62));
    view.forEach((b, i) => {
      const x = xAt(i);
      const col = b.c >= b.o ? COL.up : COL.down;
      vline(ctx, x, yP(b.h), yP(b.l), col);
      const y1 = yP(Math.max(b.o, b.c));
      const y2 = yP(Math.min(b.o, b.c));
      ctx.fillStyle = col;
      ctx.fillRect(x - body / 2, y1, body, Math.max(1, y2 - y1));
    });

    // right-axis tags
    const marks = [];
    if (inP(vwap)) marks.push({ y: yP(vwap), label: 'VWAP', bg: '#16324f', fg: COL.vwap });
    if (prof && inP(prof.poc)) marks.push({ y: yP(prof.poc), label: 'POC', bg: '#3b2e12', fg: COL.gold });
    const lastB = view[view.length - 1];
    if (inP(lastB.c)) {
      marks.push({
        y: yP(lastB.c), label: fmtPrice(lastB.c), bg: lastB.c >= lastB.o ? '#0f3d2e' : '#4a1c1f', fg: COL.text,
        line: 'rgba(215,222,232,.35)', dash: [2, 3],
      });
    }
    axisTags(ctx, w - padR + 2, marks, padT, padT + pH, padL, w - padR, padR - 4);

    // CVD pane
    let cl = Infinity;
    let ch = -Infinity;
    for (const v of cv) {
      if (v < cl) cl = v;
      if (v > ch) ch = v;
    }
    if (ch - cl < 1e-9) {
      ch += 1;
      cl -= 1;
    }
    const cp = (ch - cl) * 0.08;
    ch += cp;
    cl -= cp;
    const yC = (v) => cTop + ((ch - v) / (ch - cl)) * cH;
    hline(ctx, padL, w - padR, cTop - 6, COL.axis);
    if (cl < 0 && ch > 0) hline(ctx, padL, w - padR, yC(0), COL.axis, [2, 3]);
    ctx.save();
    ctx.strokeStyle = COL.cvd;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    cv.forEach((v, i) => {
      const x = xAt(i);
      if (i === 0 || i === sIdx) ctx.moveTo(x, yC(v));
      else ctx.lineTo(x, yC(v));
    });
    ctx.stroke();
    ctx.restore();
    ctx.fillStyle = COL.muted;
    ctx.textAlign = 'left';
    ctx.fillText('CVD', padL + 2, cTop + 6);
    ctx.fillText(fmtSigned(ch - cp), w - padR + 6, cTop + 4);
    ctx.fillText(fmtSigned(cl + cp), w - padR + 6, cTop + cH - 4);
    const lastC = cv[cv.length - 1];
    tag(ctx, w - padR + 2, Math.min(cTop + cH - 14, Math.max(cTop + 14, yC(lastC))), fmtSigned(lastC), '#2a2140', COL.cvd);

    timeLabels(ctx, view, xAt, cfg.tf, h - 7, 70, bw);
  }

  function drawZ(bars, info) {
    const g = prep($('c-z'));
    if (!g) return;
    const { ctx, w, h } = g;
    if (bars.length < cfg.zN + 2) {
      return emptyMsg(g, `สะสมข้อมูล ${bars.length}/${cfg.zN + 2} แท่ง…`);
    }
    const padL = 4;
    const padR = 30;
    const padT = 8;
    const padB = 16;
    const plotW = w - padL - padR;
    const slots = Math.max(20, Math.floor(plotW / 5));
    const i0 = Math.max(0, bars.length - slots);
    const view = bars.slice(i0);
    const zs = info.z.slice(i0);
    const bw = plotW / slots;
    const xAt = (i) => padL + (slots - view.length + i + 0.5) * bw;
    let zmax = 4;
    let zmin = -1.5;
    for (const v of zs) {
      if (v === null) continue;
      zmax = Math.max(zmax, Math.min(12, v));
      zmin = Math.min(zmin, Math.max(-4, v));
    }
    const pH = h - padT - padB;
    const y = (v) => padT + ((zmax - v) / (zmax - zmin)) * pH;
    hline(ctx, padL, w - padR, y(0), COL.axis);
    hline(ctx, padL, w - padR, y(2), 'rgba(245,166,35,.6)', [3, 3]);
    hline(ctx, padL, w - padR, y(3), 'rgba(240,85,90,.6)', [3, 3]);
    ctx.fillStyle = COL.muted;
    ctx.textAlign = 'left';
    ctx.fillText('0', w - padR + 6, y(0));
    ctx.fillText('2σ', w - padR + 6, y(2));
    ctx.fillText('3σ', w - padR + 6, y(3));
    const bwid = Math.max(1, bw * 0.7);
    view.forEach((b, i) => {
      const v = zs[i];
      if (v === null) return;
      const vv = Math.max(zmin, Math.min(zmax, v));
      const forming = i0 + i > info.ci;
      ctx.globalAlpha = forming ? 0.45 : 1;
      ctx.fillStyle = v >= 2 ? (b.buy - b.sell >= 0 ? COL.up : COL.down) : 'rgba(127,140,156,.55)';
      const y0 = y(0);
      const y1 = y(vv);
      ctx.fillRect(xAt(i) - bwid / 2, Math.min(y0, y1), bwid, Math.max(1, Math.abs(y1 - y0)));
    });
    ctx.globalAlpha = 1;
    timeLabels(ctx, view, xAt, cfg.zTf, h - 6, 64, bw);
  }

  function drawVol(info) {
    const g = prep($('c-vol'));
    if (!g) return;
    const { ctx, w, h } = g;
    const series = info.rv;
    const firstIdx = series.findIndex((v) => v !== null);
    if (firstIdx < 0) return emptyMsg(g, `ต้องมีแท่ง 1 นาทีอย่างน้อย ${RV_WINDOW + 1} แท่ง…`);
    const padL = 4;
    const padR = 44;
    const padT = 8;
    const padB = 16;
    const plotW = w - padL - padR;
    const slots = Math.max(30, Math.floor(plotW / 2));
    const start = Math.max(firstIdx, series.length - slots);
    const view = series.slice(start);
    const bw = plotW / slots;
    const xAt = (i) => padL + (slots - view.length + i + 0.5) * bw;
    let hi = -Infinity;
    let lo = Infinity;
    for (const v of view) {
      if (v === null) continue;
      hi = Math.max(hi, v);
      lo = Math.min(lo, v);
    }
    for (const v of info.q) if (Number.isFinite(v)) { hi = Math.max(hi, v); lo = Math.min(lo, v); }
    lo = Math.max(0, lo - (hi - lo) * 0.1);
    hi += (hi - lo) * 0.1 || 0.01;
    const pH = h - padT - padB;
    const y = (v) => padT + ((hi - v) / (hi - lo)) * pH;
    const [q20, q70, q90] = info.q;
    // regime bands
    const band = (a, b, color) => {
      const ya = y(Math.min(hi, b));
      const yb = y(Math.max(lo, a));
      if (yb > ya) {
        ctx.fillStyle = color;
        ctx.fillRect(padL, ya, plotW, yb - ya);
      }
    };
    if ([q20, q70, q90].every(Number.isFinite)) {
      band(lo, q20, 'rgba(124,196,255,.07)');
      band(q70, q90, 'rgba(245,166,35,.08)');
      band(q90, hi, 'rgba(240,85,90,.10)');
      ctx.fillStyle = COL.muted;
      ctx.textAlign = 'left';
      [[q20, 'P20'], [q70, 'P70'], [q90, 'P90']].forEach(([v, l]) => {
        if (v >= lo && v <= hi) {
          hline(ctx, padL, w - padR, y(v), COL.axis, [2, 3]);
          ctx.fillText(l, w - padR + 6, y(v));
        }
      });
    }
    ctx.fillStyle = COL.muted;
    ctx.fillText(fmtPct(hi), w - padR + 6, padT + 4);
    ctx.fillText(fmtPct(lo), w - padR + 6, h - padB - 4);
    ctx.save();
    ctx.strokeStyle = COL.warn;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    let started = false;
    view.forEach((v, i) => {
      if (v === null) return;
      if (!started) {
        ctx.moveTo(xAt(i), y(v));
        started = true;
      } else ctx.lineTo(xAt(i), y(v));
    });
    ctx.stroke();
    ctx.restore();
    timeLabels(ctx, info.bars.slice(start), xAt, 60, h - 6, 70, bw);
  }

  function drawProfile(rows, prof) {
    const g = prep($('c-profile'));
    if (!g) return;
    const { ctx, w, h } = g;
    if (!rows.length) {
      $('profile-hint').textContent = '–';
      return emptyMsg(g, 'ยังไม่มี volume ใน session นี้');
    }
    const padL = 4;
    const padR = 104;
    const padT = 6;
    const padB = 6;
    const plotW = w - padL - padR;
    const plotH = h - padT - padB;
    const maxRows = Math.max(10, Math.floor(plotH / 3));
    const m = Math.max(1, Math.ceil(rows.length / maxRows));
    const merged = [];
    for (const r of rows) {
      const gk = Math.floor(r.k / m);
      const last = merged[merged.length - 1];
      if (last && last.gk === gk) {
        last.buy += r.buy;
        last.sell += r.sell;
      } else merged.push({ gk, buy: r.buy, sell: r.sell });
    }
    const priceOf = (gk) => (gk * m + (m - 1) / 2) * S.step;
    const kLo = merged[0].gk;
    const kHi = merged[merged.length - 1].gk;
    const n = kHi - kLo + 1;
    const rh = plotH / n;
    const yK = (gk) => padT + (kHi - gk) * rh; // top edge of the row
    // inverse of priceOf: centre of row gk is at padT + (kHi - gk + 0.5) * rh
    const yPrice = (p) => padT + (kHi + 0.5 - (p / S.step - (m - 1) / 2) / m) * rh;
    let maxV = 0;
    for (const r of merged) maxV = Math.max(maxV, r.buy + r.sell);
    const inVA = (p) => prof && p >= prof.val - S.step * m / 2 && p <= prof.vah + S.step * m / 2;
    for (const r of merged) {
      const p = priceOf(r.gk);
      const y0 = yK(r.gk);
      const hh = Math.max(1, rh - (rh > 3 ? 1 : 0));
      const wb = (r.buy / maxV) * plotW;
      const ws = (r.sell / maxV) * plotW;
      const isPoc = prof && Math.abs(p - prof.poc) < (S.step * m) / 2 + 1e-9;
      ctx.globalAlpha = inVA(p) ? 0.9 : 0.35;
      ctx.fillStyle = isPoc ? COL.gold : COL.up;
      ctx.fillRect(padL, y0, wb, hh);
      ctx.fillStyle = isPoc ? '#c48a22' : COL.down;
      ctx.fillRect(padL + wb, y0, ws, hh);
    }
    ctx.globalAlpha = 1;
    // markers
    const { vwap } = E.vwapValue(S.sess.vwap);
    const marks = [];
    if (prof) {
      marks.push({ y: yPrice(prof.vah), label: `VAH ${fmtPrice(prof.vah)}`, bg: '#1c2531', fg: COL.muted });
      marks.push({ y: yPrice(prof.val), label: `VAL ${fmtPrice(prof.val)}`, bg: '#1c2531', fg: COL.muted });
      marks.push({ y: yPrice(prof.poc), label: `POC ${fmtPrice(prof.poc)}`, bg: '#3b2e12', fg: COL.gold });
    }
    if (Number.isFinite(vwap)) {
      marks.push({ y: yPrice(vwap), label: `VWAP ${fmtPrice(vwap)}`, bg: '#16324f', fg: COL.vwap, line: 'rgba(90,168,255,.6)' });
    }
    if (Number.isFinite(S.last)) {
      marks.push({ y: yPrice(S.last), label: fmtPrice(S.last), bg: '#2a3442', fg: COL.text, line: 'rgba(215,222,232,.5)', dash: [2, 3] });
    }
    axisTags(ctx, w - padR + 2, marks, padT, h - padB, padL, w - padR);
    $('profile-hint').textContent = `bucket ${fmtPrice(S.step * m)} · ${n} ระดับ`;
  }

  function renderBook() {
    const { bids, asks } = S.book;
    const el = $('book');
    if (!bids.length || !asks.length) {
      el.innerHTML = '<tr><td class="empty">รอข้อมูล order book…</td></tr>';
      $('book-hint').textContent = 'top 20 levels';
      return;
    }
    const n = window.innerWidth < 760 ? 8 : 12;
    const a = asks.slice(0, n);
    const b = bids.slice(0, n);
    let maxQ = 0;
    for (const l of a) maxQ = Math.max(maxQ, l[1]);
    for (const l of b) maxQ = Math.max(maxQ, l[1]);
    const row = (cls, l, cum) =>
      `<tr class="${cls}"><td class="p">${fmtPrice(l[0])}</td><td class="q">${fmtQty(l[1])}` +
      `<span class="bar" style="width:${((l[1] / maxQ) * 100).toFixed(1)}%"></span></td><td class="c">${fmtQty(cum)}</td></tr>`;
    let html = '';
    let cum = 0;
    const askRows = a.map((l) => {
      cum += l[1];
      return row('ask', l, cum);
    });
    html += askRows.reverse().join('');
    const mid = (asks[0][0] + bids[0][0]) / 2;
    const spread = asks[0][0] - bids[0][0];
    html += `<tr class="mid"><td colspan="3">spread ${fmtPrice(spread)} · ${((spread / mid) * 1e4).toFixed(2)} bp</td></tr>`;
    cum = 0;
    html += b.map((l) => {
      cum += l[1];
      return row('bid', l, cum);
    }).join('');
    el.innerHTML = html;
    const span = (asks[asks.length - 1][0] - bids[bids.length - 1][0]) / 2 / mid * 1e4;
    $('book-hint').textContent = `${bids.length}/${asks.length} levels · ±${span.toFixed(1)} bp`;
    const sb = bids.reduce((s, l) => s + l[1], 0);
    const sa = asks.reduce((s, l) => s + l[1], 0);
    setVal('obi5', fmtNum(S.obi5, 2), signCls(S.obi5));
    setVal('obi20', fmtNum(S.obi20, 2), signCls(S.obi20));
    setVal('bidask', sa > 0 ? (sb / sa).toFixed(2) : '–', sb > sa ? 'pos' : sb < sa ? 'neg' : '');
  }

  function drawObi() {
    const g = prep($('c-obi'));
    if (!g) return;
    const { ctx, w, h } = g;
    const hist = S.obiHist;
    if (hist.length < 2) return;
    const y = (v) => h / 2 - (v * (h - 4)) / 2;
    hline(ctx, 0, w, y(0), COL.axis);
    const bw = w / 300;
    const x0 = w - hist.length * bw;
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(x0, y(0));
    hist.forEach((v, i) => ctx.lineTo(x0 + i * bw, y(v)));
    ctx.lineTo(x0 + (hist.length - 1) * bw, y(0));
    ctx.closePath();
    ctx.clip();
    ctx.fillStyle = 'rgba(34,192,138,.35)';
    ctx.fillRect(0, 0, w, y(0));
    ctx.fillStyle = 'rgba(240,85,90,.35)';
    ctx.fillRect(0, y(0), w, h - y(0));
    ctx.restore();
    ctx.fillStyle = COL.muted;
    ctx.textAlign = 'left';
    ctx.fillText('OBI EMA · 5 นาที', 4, 8);
  }

  function setVal(id, text, cls) {
    const el = $(id);
    if (el.textContent !== text) el.textContent = text;
    const c = cls || '';
    if (el.dataset.cls !== c) {
      if (el.dataset.cls) el.classList.remove(el.dataset.cls);
      if (c) el.classList.add(c);
      el.dataset.cls = c;
    }
  }

  function renderKpis(zInfo, vol, prof) {
    const s = S.sess;
    const p = S.last;
    setVal('k-price', fmtPrice(p));
    const chg = p - s.open;
    setVal('k-chg', Number.isFinite(chg) ? `${fmtNum(chg, S.pd)} (${fmtNum((chg / s.open) * 100, 2)}%)` : '–', signCls(chg));
    const hasHL = Number.isFinite(s.high) && Number.isFinite(s.low);
    setVal('k-hl', hasHL ? fmtPrice(s.high) : '–');
    setVal('k-range', hasHL ? `L ${fmtPrice(s.low)} · R ${fmtPrice(s.high - s.low)} · เริ่ม ${fmtClock(s.start * 1000, false)}` : '–');
    const { vwap, sd } = E.vwapValue(s.vwap);
    setVal('k-vwap', fmtPrice(vwap));
    const dist = sd > 0 ? (p - vwap) / sd : NaN;
    setVal('k-vwapd', Number.isFinite(dist) ? `${fmtNum(dist, 2)}σ · 1σ ${fmtPrice(sd)}` : '–', signCls(dist));
    setVal('k-poc', prof ? fmtPrice(prof.poc) : '–');
    setVal('k-va', prof ? `VA ${fmtPrice(prof.val)}–${fmtPrice(prof.vah)}` : '–');
    setVal('k-vol', fmtQty(s.vol));
    setVal('k-volu', Number.isFinite(vwap) ? `${baseAsset()} · ≈ $${fmtQty(s.vol * vwap)}` : baseAsset());
    setVal('k-delta', fmtSigned(s.delta), signCls(s.delta));
    setVal('k-deltap', s.vol > 0 ? `${fmtNum((s.delta / s.vol) * 100, 1)}% ของ volume` : '–', signCls(s.delta));
    const tps = S.tradeTimes.length / 10;
    setVal('k-tps', S.connectedAt ? tps.toFixed(1) : '–');
    setVal('k-avgsz', S.sizes.length ? `avg size ${fmtQty(S.sizeSum / S.sizes.length)}` : '–');
    setVal('k-rv', fmtPct(vol.cur));
    setVal('k-reg', vol.regime === 'n/a' ? 'ข้อมูลยังไม่พอ' : `${vol.regime} · P${vol.pct.toFixed(0)}`, `rg-${vol.regime}`);
    setVal('k-z', zInfo.cur === null || zInfo.cur === undefined ? '–' : fmtNum(zInfo.cur, 2),
      zInfo.cur >= 2 ? 'pos' : '');
    setVal('k-zs', zInfo.cur === null && zInfo.ci < cfg.zN
      ? `สะสม ${Math.max(0, zInfo.ci + 1)}/${cfg.zN + 1} แท่ง`
      : `${tfLabel(cfg.zTf)} vs prior ${cfg.zN}`);
    setVal('k-obi', fmtNum(S.obi20, 2), signCls(S.obi20));
    setVal('k-obis', Number.isFinite(S.obiEma) ? `EMA ${fmtNum(S.obiEma, 2)}` : '–');
    // volatility panel stats
    setVal('v-rv', fmtPct(vol.cur));
    setVal('v-pct', Number.isFinite(vol.pct) ? `P${vol.pct.toFixed(0)} · ${vol.regime}` : '–', `rg-${vol.regime}`);
    setVal('v-pk', fmtPct(vol.pk));
    setVal('v-atr1', Number.isFinite(vol.atr1) ? fmtPrice(vol.atr1) : '–');
    setVal('v-atrtf', Number.isFinite(vol.atrTf) ? `${fmtPrice(vol.atrTf)} (${tfLabel(cfg.tf)})` : '–');
    setVal('v-rng', Number.isFinite(vol.rng) ? `${vol.rng.toFixed(2)}×` : '–', vol.rng >= 1.5 ? 'neg' : '');
  }

  function renderTape() {
    if (S.rendered.tape === S.tradeVer && S.rendered.tapeMult === cfg.bigMult) return;
    S.rendered.tape = S.tradeVer;
    S.rendered.tapeMult = cfg.bigMult;
    const rows = [];
    for (let i = S.trades.length - 1; i >= 0 && rows.length < 80; i--) {
      const t = S.trades[i];
      if (cfg.bigMult === 1 || t.mult >= cfg.bigMult) rows.push(t);
    }
    const el = $('tape');
    if (!rows.length) {
      el.innerHTML = `<div class="empty">${S.trades.length ? `ยังไม่มีเทรด ≥ ${cfg.bigMult}× ค่าเฉลี่ย` : 'รอข้อมูล trade…'}</div>`;
      return;
    }
    el.innerHTML = '<table>' + rows.map((t) =>
      `<tr class="${t.sellAgg ? 'sell' : 'buy'}${t.mult >= 10 ? ' huge' : ''}"><td class="mut">${fmtClock(t.T)}</td>` +
      `<td class="side">${t.sellAgg ? 'SELL' : 'BUY'}</td><td class="r">${fmtPrice(t.p)}</td>` +
      `<td class="r">${fmtQty(t.q)}</td><td class="r mut">${Number.isFinite(t.mult) ? t.mult.toFixed(1) + '×' : ''}</td></tr>`
    ).join('') + '</table>';
  }

  function renderSignals() {
    if (S.rendered.sig === S.sigVer) return;
    S.rendered.sig = S.sigVer;
    const el = $('signals');
    if (!S.signals.length) {
      el.innerHTML = '<div class="empty">ยังไม่มีสัญญาณ — จะแสดงเมื่อ volume ผิดปกติ, มีเทรดใหญ่, CVD divergence หรือ volatility เปลี่ยน regime</div>';
      return;
    }
    el.innerHTML = S.signals.map((s) =>
      `<div class="sig ${s.side}"><span class="t num">${fmtClock(s.t)}</span><span class="k">${esc(s.title)}</span><span>${esc(s.detail)}</span></div>`
    ).join('');
  }

  function updateStatus() {
    const now = Date.now();
    const dot = $('dot');
    let cls = '';
    let text;
    if (!S.expected) text = 'กำลังโหลดประวัติ…';
    else if (S.open < S.expected) text = S.connectedAt ? 'กำลังเชื่อมต่อใหม่…' : 'กำลังเชื่อมต่อ…';
    else if (!S.lastMsg || now - S.lastMsg > 15000) {
      text = 'เชื่อมต่อแล้ว · ไม่มีข้อมูลเข้า';
      cls = 'err';
    } else {
      text = `Live · ${cfg.symbol}`;
      cls = 'live';
    }
    dot.className = 'dot' + (cls ? ' ' + cls : '');
    setVal('status', text);
    if (S.startedAt && !S.lastMsg && !S.noDataWarned && !S.seedError && now - S.startedAt > 20000) {
      S.noDataWarned = true;
      showBanner('ยังไม่ได้รับข้อมูลจาก Binance WebSocket — ตรวจสอบว่าเครือข่ายนี้เข้าถึง Binance ได้ และสัญลักษณ์มีในตลาดที่เลือก');
    }
  }

  function render() {
    if (!S || !S.sess) return;
    const nowSec = Math.floor((Date.now() + S.skew) / 1000);
    const chartBars = barsFor(cfg.tf, nowSec);
    const zBars = cfg.zTf === cfg.tf ? chartBars : barsFor(cfg.zTf, nowSec);
    const minBars = cfg.tf === 60 ? chartBars : barsFor(60, nowSec);
    const rows = profileRows();
    const prof = rows.length
      ? E.valueArea(rows.map((r) => ({ price: r.k * S.step, vol: r.buy + r.sell })), 0.7)
      : null;
    const zInfo = computeZ(zBars, nowSec);
    const vol = computeVol(minBars, chartBars);
    drawChart(chartBars, nowSec, prof);
    drawZ(zBars, zInfo);
    drawVol(vol);
    drawProfile(rows, prof);
    renderBook();
    drawObi();
    renderKpis(zInfo, vol, prof);
    renderTape();
    renderSignals();
    updateStatus();
  }

  // ---------------------------------------------------------------- UI wiring
  function showBanner(text) {
    const b = $('banner');
    b.textContent = text;
    b.classList.add('show');
  }
  function hideBanner() {
    $('banner').classList.remove('show');
  }

  function syncControls() {
    document.querySelectorAll('#market button').forEach((b) => b.classList.toggle('on', b.dataset.v === cfg.market));
    $('symbol').value = cfg.symbol;
    $('symbols').innerHTML = PRESETS[cfg.market].map(([s, l]) => `<option value="${s}">${esc(l)}</option>`).join('');
    document.querySelectorAll('#tf button').forEach((b) => b.classList.toggle('on', +b.dataset.v === cfg.tf));
    document.querySelectorAll('#bigmult button').forEach((b) => b.classList.toggle('on', +b.dataset.v === cfg.bigMult));
    $('anchor').value = cfg.anchor;
    $('ztf').value = String(cfg.zTf);
    $('zn').value = String(cfg.zN);
  }

  function initControls() {
    $('tf').innerHTML = TFS.map(([v, l]) => `<button data-v="${v}">${l}</button>`).join('');
    $('ztf').innerHTML = Z_TFS.map(([v, l]) => `<option value="${v}">${l}</option>`).join('');

    $('market').addEventListener('click', (e) => {
      const v = e.target.dataset && e.target.dataset.v;
      if (!isMarket(v) || v === cfg.market) return;
      cfg.market = v;
      cfg.symbol = PRESETS[v][0][0];
      start();
    });
    const sym = $('symbol');
    const applySymbol = () => {
      const s = cleanSymbol(sym.value);
      if (!s) {
        sym.value = cfg.symbol;
        return;
      }
      if (s !== cfg.symbol) {
        cfg.symbol = s;
        start();
      }
    };
    sym.addEventListener('change', applySymbol);
    sym.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') sym.blur();
    });
    $('tf').addEventListener('click', (e) => {
      const v = +(e.target.dataset && e.target.dataset.v);
      if (!v || v === cfg.tf) return;
      cfg.tf = v;
      S.divBarT = null;
      S.divState = null;
      saveSettings();
      syncControls();
      render();
    });
    $('anchor').addEventListener('change', (e) => {
      cfg.anchor = e.target.value === 'ny' ? 'ny' : 'utc';
      start();
    });
    $('ztf').addEventListener('change', (e) => {
      cfg.zTf = +e.target.value;
      S.zBarT = null;
      saveSettings();
      render();
    });
    $('zn').addEventListener('change', (e) => {
      cfg.zN = +e.target.value;
      S.zBarT = null;
      saveSettings();
      render();
    });
    $('bigmult').addEventListener('click', (e) => {
      const v = +(e.target.dataset && e.target.dataset.v);
      if (!v) return;
      cfg.bigMult = v;
      saveSettings();
      syncControls();
      renderTape();
    });
    window.addEventListener('resize', () => render());
  }

  initControls();
  start();
  setInterval(() => {
    if (!document.hidden) render();
  }, 250);
})();
