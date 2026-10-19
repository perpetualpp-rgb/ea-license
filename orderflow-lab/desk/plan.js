/* Order Flow Lab desk plan — the pure rules behind the "แผน" table: levels, zones, entry/SL/TP and bias.
 *
 * desk/build.py inlines this file into the page (window.OFP) after engine.js (OF) and journal.js (OFJ);
 * desk/backtest.js runs the same file under node over historical candles. Nothing here reads page state:
 * the page passes what it has (order-book walls, big-trade zones, live buy pressure, the OI read) in ctx.
 *
 *   bar = {t (unix s, bar start), o, h, l, c, v, bv}   bv = taker-buy volume, NaN when unknown
 *   ins = {src: "bin"|"tdv", sessOff (s, Binance), tick, round}
 */
(function (root, factory) {
  const P = factory(root.OF || require("../web/engine.js"), root.OFJ || require("./journal.js"));
  if (typeof module !== "undefined" && module.exports) module.exports = P;
  root.OFP = P;
})(typeof globalThis !== "undefined" ? globalThis : this, function (OF, OFJ) {
  "use strict";

  // day: risk sized by the 1-hour ATR; swing: by the 4-hour ATR with wider targets
  // stops and zones scale with the daily ATR so quiet hours (weekends, Asia) don't shrink them
  // confirmation, lock and timeout rules live in journal.js (OFJ.RULES) so the table and the runner share them
  const PLAN = { day: { ...OFJ.RULES.styles.day, label: "เดย์เทรด", slK: 0.25, clK: 0.10, floor: 0.002, minR: [1.5, 2.5], rounds: 4 },
                 swing: { ...OFJ.RULES.styles.swing, label: "สวิง", slK: 0.5, clK: 0.15, floor: 0.004, minR: [2, 3.5], rounds: 8 } };

  // ---- per-bar flow (exact when the bar carries taker-buy volume) -------------------------
  const exact = b => isFinite(b.bv);
  function split(b) { if (exact(b)) return [b.bv, Math.max(0, b.v - b.bv)]; const r = b.h - b.l, f = r > 0 ? (b.c - b.l) / r : 0.5; return [b.v * f, b.v * (1 - f)]; }
  const delta = b => { const [x, y] = split(b); return x - y; };

  // ---- sessions -----------------------------------------------------------------------
  function sessions(bs, ins) {
    const out = []; let cur = [], key = null;
    for (let i = 0; i < bs.length; i++) {
      const b = bs[i], k = ins.src === "bin" ? Math.floor((b.t - ins.sessOff) / 86400) : null;
      const brk = i > 0 && (ins.src === "bin" ? k !== key : b.t - bs[i - 1].t >= 2400);
      if (brk && cur.length) { out.push(cur); cur = []; }
      cur.push(b); key = k;
    }
    if (cur.length) out.push(cur);
    return out;
  }
  // a Binance gold session that falls on a Saturday or Sunday trading day (gold itself is closed then)
  const weekendSession = (seg, ins) => { const d = new Date((seg[0].t - ins.sessOff + 86400) * 1000).getUTCDay(); return d === 0 || d === 6; };
  function derive(sess, ins) {
    if (sess.length < 5) return null;
    const ss = sessions(sess, ins), cur = ss[ss.length - 1];
    // the previous session gives the Range lanes; for gold on Binance (open all weekend) skip the weekend ones,
    // so Monday reads Friday's range rather than a thin Sunday
    let prev = null;
    for (let i = ss.length - 2; i >= 0 && !prev; i--) if (!(ins.gold && ins.src === "bin" && weekendSession(ss[i], ins))) prev = ss[i];
    const prof = OF.profileFromBars(cur, ins.tick), rng = OF.sessionRange(prof, 0.70), mag = OF.valueArea(prof, 0.30);
    let pr = null;
    if (prev && prev.length > 10) { const pp = OF.profileFromBars(prev, ins.tick), r = OF.sessionRange(pp, 0.70), m = OF.valueArea(pp, 0.30);
      pr = { yhi: Math.max(...prev.map(b => b.h)), ylo: Math.min(...prev.map(b => b.l)), poc: r.poc, vah: r.va_high, val: r.va_low, mhi: m[1], mlo: m[0] }; }
    return { cur, prev, prof, rng, mag, pr, last: cur[cur.length - 1] };
  }

  function atrDaily(bars) {
    const d = bars.length > 4 ? bars.slice(0, -1) : bars; if (d.length < 3) return null; // drop the forming day
    const tr = d.slice(1).map((x, i) => Math.max(x.h - x.l, Math.abs(x.h - d[i].c), Math.abs(x.l - d[i].c))).slice(-14);
    return tr.reduce((a, v) => a + v, 0) / tr.length;
  }
  function atrOf(bars, sec) {
    const g = new Map();
    for (const b of bars) { const k = Math.floor(b.t / sec), x = g.get(k); if (!x) g.set(k, { h: b.h, l: b.l, c: b.c }); else { x.h = Math.max(x.h, b.h); x.l = Math.min(x.l, b.l); x.c = b.c; } }
    let hs = [...g.entries()].sort((a, z) => a[0] - z[0]).map(e => e[1]); if (hs.length > 4) hs = hs.slice(0, -1); // drop the forming bar
    if (hs.length < 3) return null;
    const tr = hs.slice(1).map((x, i) => Math.max(x.h - x.l, Math.abs(x.h - hs[i].c), Math.abs(x.l - hs[i].c))).slice(-14);
    return tr.reduce((a, v) => a + v, 0) / tr.length;
  }

  /**
   * ctx = {ins, D (derive), daily (1D bars, forming day last), sess (5m/1m bars), last,
   *        walls: [{p, name}] persistent order-book walls, big: [{p, name}] big-trade zones,
   *        press: buy share of the last 5 minutes in % (null when unknown), oi: {sign, label} (null when unknown)}
   */
  function planLevels(ctx, last, st) {
    const L = [], add = (p, name, w) => { if (p != null && isFinite(p)) L.push({ p, name, w }); }, { D, ins } = ctx;
    if (D.pr) { add(D.pr.yhi, "Y-Hi", 1.5); add(D.pr.vah, "Top zone", 1.5); add(D.pr.mhi, "Magnet บน", 1); add(D.pr.poc, "POC ก่อน", 1.5); add(D.pr.mlo, "Magnet ล่าง", 1); add(D.pr.val, "Bottom zone", 1.5); add(D.pr.ylo, "Y-Lo", 1.5); }
    const dd = ctx.daily.length > 1 ? ctx.daily.slice(0, -1) : [];
    for (const [n, w] of [[5, 1.5], [20, 1.25]]) if (dd.length >= n) { const z = dd.slice(-n); add(Math.max(...z.map(b => b.h)), `สูงสุด ${n} วัน`, w); add(Math.min(...z.map(b => b.l)), `ต่ำสุด ${n} วัน`, w); }
    const vw = OF.vwapBands(D.cur, [2, 3]), v = vw[vw.length - 1];
    if (v) { add(v.vwap, "VWAP", 1); add(v.bands[2][1], "Assist +2σ", 1); add(v.bands[3][1], "Assist +3σ", 1); add(v.bands[2][0], "Assist −2σ", 1); add(v.bands[3][0], "Assist −3σ", 1); }
    add(D.rng.poc, "POC วันนี้", 1);
    for (const w of ctx.walls || []) add(w.p, w.name, 2.5);
    for (const z of ctx.big || []) add(z.p, z.name, 1.5);
    const r0 = Math.floor(last / ins.round) * ins.round; for (let k = -st.rounds + 1; k <= st.rounds; k++) add(r0 + k * ins.round, "เลขกลม", 0.75);
    return { L, v };
  }
  function cluster(levels, side, last, win) {
    // candidate zones on one side of price, scored by the weight of levels stacked within win
    const c = levels.filter(l => side === "res" ? l.p > last + 0.5 * win : l.p < last - 0.5 * win).sort((a, z) => Math.abs(a.p - last) - Math.abs(z.p - last));
    let best = null;
    for (const seed of c.slice(0, 6)) {
      const mem = c.filter(l => Math.abs(l.p - seed.p) <= win), score = mem.reduce((s, l) => s + l.w, 0) - Math.abs(seed.p - last) / win * 0.15;
      if (!best || score > best.score) best = { score, mem, lo: Math.min(...mem.map(l => l.p)), hi: Math.max(...mem.map(l => l.p)) };
    }
    return best;
  }
  function biasModel(ctx, last, v) {
    const cvd = ctx.D.cur.reduce((a, b) => a + delta(b), 0); let score = 0; const why = [];
    if (v) { if (last > v.vwap) { score++; why.push("ราคาเหนือ VWAP"); } else { score--; why.push("ราคาใต้ VWAP"); } }
    if (cvd > 0) { score++; why.push("CVD วันนี้เป็นบวก"); } else if (cvd < 0) { score--; why.push("CVD วันนี้เป็นลบ"); }
    const pr = ctx.press;
    if (pr != null) { if (pr >= 55) { score++; why.push(`แรงซื้อ 5 นาที ${pr.toFixed(0)}%`); } else if (pr <= 45) { score--; why.push(`แรงขาย 5 นาที ${(100 - pr).toFixed(0)}%`); } }
    const o = ctx.oi; if (o && o.sign) { score += o.sign; why.push(`OI 1 ชม.: ${o.label}`); }
    return { score, why, bias: score >= 2 ? "เอียงซื้อ" : score <= -2 ? "เอียงขาย" : "สองทาง รอราคาเข้าโซน" };
  }

  function barIndexAt(bars, tsSec) { let lo = 0, hi = bars.length - 1; if (hi < 0 || tsSec < bars[0].t) return -1;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (bars[m].t <= tsSec) lo = m; else hi = m - 1; } return lo; }
  /** price change vs open-interest change over the same window; oi = [{ts (ms), v}] oldest first, sess = the bars that price it */
  function oiRead(oi, sess, hours) {
    if (oi.length < 2) return null;
    const lastO = oi[oi.length - 1], ref = [...oi].reverse().find(o => o.ts <= lastO.ts - hours * 3600000); if (!ref) return null;
    const px = ts => { const i = barIndexAt(sess, ts / 1000); return i >= 0 ? sess[i].c : null; }, p1 = px(lastO.ts), p0 = px(ref.ts);
    const dOi = (lastO.v - ref.v) / ref.v * 100, dPx = p0 && p1 ? (p1 - p0) / p0 * 100 : null;
    let label = "ทรงตัว", sign = 0;
    if (dPx != null && Math.abs(dOi) >= 0.3) {
      if (dPx > 0.05) [label, sign] = dOi > 0 ? ["Long ใหม่เข้า", 1] : ["Short ปิดหนี", 0];
      else if (dPx < -0.05) [label, sign] = dOi > 0 ? ["Short ใหม่เข้า", -1] : ["Long ปิดหนี", 0];
      else label = dOi > 0 ? "OI เพิ่ม ราคานิ่ง" : "OI ลด ราคานิ่ง";
    }
    return { dOi, dPx, label, sign };
  }

  // ---- plan model (shared by the table, the journal and the back-test) -------------------------------
  function planModel(ctx, styleKey) {
    const { ins, D, last } = ctx, st = PLAN[styleKey];
    if (!D || last == null) return { err: "รอข้อมูล" };
    const dAtr = atrDaily(ctx.daily), hAtr = atrOf(ctx.sess, 3600), atr = dAtr || (hAtr ? hAtr * 4 : null);
    if (!atr) return { err: "ข้อมูลย้อนหลังยังไม่พอคำนวณ ATR" };
    const slGap = Math.max(st.slK * atr, last * st.floor), win = st.clK * atr, gap2 = Math.max(win, 0.25 * atr), { L, v } = planLevels(ctx, last, st);
    const B = biasModel(ctx, last, v), plans = {};
    for (const [side, z] of [["sell", cluster(L, "res", last, win)], ["buy", cluster(L, "sup", last, win)]]) {
      if (!z) continue; const sell = side === "sell", dir = sell ? -1 : 1, entry = sell ? z.lo : z.hi, stop = sell ? z.hi + slGap : z.lo - slGap, risk = Math.abs(entry - stop);
      const ahead = L.filter(l => (l.p - entry) * dir > 0).sort((a, b) => Math.abs(a.p - entry) - Math.abs(b.p - entry));
      const pick = (minR, after) => ahead.find(l => Math.abs(l.p - entry) >= minR * risk && (after == null || Math.abs(l.p - entry) >= Math.abs(after - entry) + gap2))
        || { p: entry + dir * Math.max(minR * risk, after == null ? 0 : Math.abs(after - entry) + gap2), name: `${minR}R (ไม่มีแนวถัดไป)` };
      const t1 = pick(st.minR[0], null), t2 = pick(st.minR[1], t1.p);
      plans[side] = { inst: ins.key, style: styleKey, side, zoneLo: z.lo, zoneHi: z.hi, entry, stop, risk, tp1: t1.p, tp1Name: t1.name, tp2: t2.p, tp2Name: t2.name,
                      names: [...new Set(z.mem.map(l => l.name))].join(" + "), trend: B.score === 0 ? null : (sell ? B.score < 0 : B.score > 0) };
    }
    return { st, atr, atrName: dAtr ? "ATR รายวัน" : "ATR รายวัน ≈ (ATR 1 ชม. × 4)", slGap, last, ...B, plans };
  }

  // ---- order-flow checklist (the owner's The Trader X notes) ---------------------------------------------------
  // One side for the day from CVD (red: sells only, green: buys only), the zone on that side where levels stack,
  // then the checks before an entry; the entry is the close of the trigger candle, SL beyond the zone by slBuf
  // dollars, TP1 VWAP or the first real wall, TP2 the far Assist edge / a wall / Magnet, TP3 a big opposite wall.
  const OF_REQUIRED = ["cvd", "cloud", "expand", "lane", "wall", "bigx", "trigger", "flow", "sltp", "news"];
  const OF_SOFT = new Set(["lane", "news"]);   // "warn" there (not known / outside the main hours) does not block
  const OF_LABEL = {
    cvd: "CVD ใต้น้ำหรือเหนือน้ำ · ไม้ไปทางเดียวกับ CVD", cloud: "ราคาอยู่โซนแพง/ถูกของ Assist (ไม่ใช่กลางๆ)",
    expand: "Assist ไม่ได้ระเบิดสวนทางไม้", lane: "เลนของ Range · ไม้นี้เล่นในเลนเดียวกัน", round: "มีเลขกลมตรงโซน",
    wall: "Wall ที่ยืนนาน (S/เส้นทึบ) อยู่ที่โซน และกำแพงยังไม่หด", book: "Order Book ที่โซน ≥ 2SD และติด (+)",
    bigx: "ไม่มี Big Trade ✕ (โซนต่อสู้) ที่โซน", trigger: "แท่ง 5 นาทีปิดสีตรงข้ามในโซน หรือ 15 นาทีทิ้งหาง",
    flow: "Flow: Net / Pressure ไปทางเดียวกับไม้", sltp: "SL / TP1 / TP2 ครบ ขนาดไม้คิดจากระยะ SL", news: "ช่วงเวลาและข่าว",
  };
  const OF_ORDER = ["cvd", "cloud", "expand", "lane", "round", "wall", "book", "bigx", "trigger", "flow", "sltp", "news"];

  /** keeps the zone of a side once the price is near it (the plan's zone search skips levels at the price) */
  function ofZone(prev, M, side, last, now, o = {}) {
    const radius = (o.radius || 1.5) * (M && M.slGap || 0), maxMs = o.maxMs || 4 * 3600000;
    if (prev && prev.side === side && now - prev.at <= maxMs && last >= prev.lo - 2 * radius && last <= prev.hi + 2 * radius) return prev;
    const p = M && M.plans && M.plans[side]; if (!p) return null;
    const edge = side === "sell" ? p.zoneLo : p.zoneHi;
    return { side, lo: p.zoneLo, hi: p.zoneHi, names: p.names, at: now, locked: Math.abs(last - edge) <= radius };
  }

  /**
   * c = {ins, D, last, now (ms), sess (5m bars, oldest first), zone ({side, lo, hi, names} from ofZone),
   *      walls (WallTracker.active()), book ({bids, asks} bucketed), prevBook ({bid: Map, ask: Map}), bucket,
   *      bigZones ([{price, buy, sell, contested}]), zoneTick, tape ({net5, pressure} or null), news ({status, title})}
   */
  function checklist(c, opt = {}) {
    const o = { slBuf: 6, roundTol: 1.5, ...opt }, { ins, D, last, now } = c, items = {};
    const set = (id, state, detail) => { items[id] = { id, label: OF_LABEL[id], state, detail, required: OF_REQUIRED.includes(id), soft: OF_SOFT.has(id) }; };
    if (!D || last == null) return { err: "รอข้อมูล" };
    const cvd = D.cur.reduce((a, b) => a + delta(b), 0), side = cvd < 0 ? "sell" : cvd > 0 ? "buy" : null, sell = side === "sell", dir = sell ? -1 : 1;
    const f = v => v == null || !isFinite(v) ? "–" : v.toLocaleString("en-US", { maximumFractionDigits: 2 });
    set("cvd", side ? "pass" : "fail", side ? `CVD session นี้ ${cvd > 0 ? "+" : "−"}${f(Math.abs(cvd))} ${sell ? "ใต้น้ำ → หาแต่จุดขาย" : "เหนือน้ำ → หาแต่จุดซื้อ"}` : "CVD เป็นศูนย์ ยังไม่มีขา");
    const z = c.zone && side && c.zone.side === side ? c.zone : null;
    // closed candles only: the last 5-minute one and the last whole 15-minute one
    const closed = c.sess.filter(b => (b.t + 300) * 1000 <= now), b5 = closed[closed.length - 1] || null;
    let b15 = null;
    if (closed.length) { const k = Math.floor((closed[closed.length - 1].t + 300) / 900) * 900 - 900, part = closed.filter(b => b.t >= k && b.t < k + 900);
      if (part.length === 3) b15 = { t: k, o: part[0].o, h: Math.max(...part.map(b => b.h)), l: Math.min(...part.map(b => b.l)), c: part[2].c }; }
    // Assist: where the price sits in sigma, and whether the band is blowing out
    const vw = OF.vwapBands(D.cur, [1, 2, 3]), v = vw[vw.length - 1], shape = OF.bandShape(vw);
    if (!v || !side) { set("cloud", "fail", "ยังไม่มี VWAP"); set("expand", "fail", "—"); }
    else {
      const ref = sell ? Math.max(last, b5 ? b5.h : last) : Math.min(last, b5 ? b5.l : last), zs = v.sd > 0 ? (ref - v.vwap) / v.sd : 0, zz = zs * dir * -1;
      set("cloud", zz >= 2 ? "pass" : zz >= 1 ? "warn" : "fail", `${sell ? "สูงสุดล่าสุด" : "ต่ำสุดล่าสุด"} อยู่ที่ ${zs >= 0 ? "+" : "−"}${Math.abs(zs).toFixed(2)}σ จาก VWAP ${f(v.vwap)} · ${sell ? "ขายในเมฆบน (≥ +2σ)" : "ซื้อในเมฆล่าง (≤ −2σ)"}`);
      const up = last > v.vwap, against = shape === "expanding" && (sell ? up : !up);
      set("expand", against ? "fail" : "pass", shape === "expanding" ? (against ? `เมฆระเบิด${up ? "ขึ้น" : "ลง"} ห้ามสวน` : "เมฆระเบิดไปทางเดียวกับไม้") : shape === "flat" ? "เมฆแบน (sideway) เล่นกลับค่ากลางได้" : "เมฆขยายตัวเล็กน้อย");
    }
    // Range lanes of the previous session
    const pr = D.pr;
    if (!pr) set("lane", "warn", "ยังไม่มี Range ของ session ก่อน");
    else {
      const lane = last > pr.yhi ? "เหนือ Y-Hi" : last >= pr.vah ? "Top Zone" : last > pr.mhi ? "เหนือ Magnet" : last >= pr.mlo ? "Magnet" : last > pr.val ? "ใต้ Magnet" : last >= pr.ylo ? "Bottom Zone" : "ใต้ Y-Lo";
      const bad = side && (sell ? last < pr.val : last >= pr.vah);
      set("lane", !side ? "fail" : bad ? "fail" : "pass", `ราคาอยู่ ${lane}${bad ? (sell ? " · ไม่ขายในโซนล่าง" : " · Top Zone อันตราย ไม่ซื้อ") : ""}`);
    }
    const tol = 2 * (c.bucket || 0);
    const atZone = p => z && p >= z.lo - tol && p <= z.hi + tol;
    if (!z) for (const id of ["round", "wall", "book", "bigx", "trigger"]) set(id, "fail", "ยังไม่มีโซนที่หลายระดับซ้อนกันฝั่งนี้");
    else {
      const step = ins.round, r0 = Math.ceil((z.lo - o.roundTol) / step) * step, hit = r0 <= z.hi + o.roundTol ? r0 : null;
      set("round", hit != null ? "pass" : "warn", hit != null ? `เลขกลม ${f(hit)} อยู่ในโซน` : "ไม่มีเลขกลมในโซน (มีน้ำหนักเมื่อซ้อนกับสัญญาณอื่นเท่านั้น)");
      const ws = (c.walls || []).filter(w => w.side === (sell ? "ask" : "bid") && atZone(w.price)).sort((a, b) => b.size - a.size);
      const strong = ws.find(w => w.persistent && w.status !== "pulling"), pulling = ws.find(w => w.status === "pulling");
      set("wall", strong ? "pass" : "fail", strong ? `${sell ? "Ask" : "Bid"} ${f(strong.size)} ที่ ${f(strong.price)} ยืน ${Math.round(strong.age_ms / 60000)} นาที (${strong.status === "adding" ? "เติม" : "ยืน"})`
        : pulling ? `กำแพง ${f(pulling.price)} กำลังหด (ถอนออเดอร์)` : ws.length ? "มีกำแพงแต่ยังใหม่ (N/เส้นประ) ถอนง่าย" : "ไม่มีกำแพงฝั่งนี้ที่โซน");
      const lv = c.book ? (sell ? c.book.asks : c.book.bids) : [], vals = lv.slice(0, 200).map(l => l[1]);
      if (!vals.length) set("book", "warn", "ไม่มี Order Book");
      else {
        const m = vals.reduce((a, x) => a + x, 0) / vals.length, sd = Math.sqrt(vals.reduce((a, x) => a + (x - m) * (x - m), 0) / vals.length) || 1;
        const best = lv.filter(l => atZone(l[0])).map(l => ({ p: l[0], q: l[1], z: (l[1] - m) / sd })).sort((a, b) => b.z - a.z)[0];
        const prev = best && c.prevBook ? (c.prevBook[sell ? "ask" : "bid"].get(best.p) || 0) : null, chg = best && prev != null ? best.q - prev : null;
        set("book", best && best.z >= 2 && (chg == null || chg >= 0) ? "pass" : best && best.z >= 2 ? "fail" : "warn",
          best ? `ใหญ่สุดในโซน ${f(best.q)} ที่ ${f(best.p)} = ${best.z.toFixed(1)}SD${chg == null ? "" : chg > 0 ? ` (+${f(chg)})` : chg < 0 ? ` (−${f(-chg)} เริ่มถอด)` : " (คงที่)"}` : "ไม่มีระดับใน book ที่โซน");
      }
      const bz = (c.bigZones || []).filter(b => b.price >= z.lo - (c.zoneTick || 0) && b.price <= z.hi + (c.zoneTick || 0));
      const fight = bz.find(b => b.contested), opp = bz.find(b => (sell ? b.buy > b.sell * 2 : b.sell > b.buy * 2));
      set("bigx", fight ? "fail" : opp ? "warn" : "pass", fight ? `ไม้ใหญ่สองฝั่งปะทะที่ ${f(fight.price)} = โซนต่อสู้ รอดูผล` : opp ? `ไม้ใหญ่ฝั่ง${sell ? "ซื้อ" : "ขาย"}เดี่ยวที่ ${f(opp.price)} ระวังวิ่งแรง/กวาด` : bz.length ? "ไม้ใหญ่ที่โซนอยู่ฝั่งเดียวกับไม้" : "ไม่มีไม้ใหญ่ที่โซน");
    }
    // the trigger: a closed 5m candle into the zone in the trade's colour, or a 15m candle with a wick off it
    let trig = null;
    if (z && b5 && (sell ? b5.h >= z.lo && b5.c < b5.o : b5.l <= z.hi && b5.c > b5.o)) trig = { kind: "5m", bar: b5, sec: 300 };
    else if (z && b15) { const rng = b15.h - b15.l, wick = sell ? b15.h - Math.max(b15.o, b15.c) : Math.min(b15.o, b15.c) - b15.l;
      if (rng > 0 && wick >= 0.5 * rng && (sell ? b15.h >= z.lo : b15.l <= z.hi)) trig = { kind: "15m", bar: b15, sec: 900 }; }
    if (z) set("trigger", trig ? "pass" : "fail", trig ? `แท่ง ${trig.kind} ปิด ${f(trig.bar.c)} ${trig.kind === "5m" ? (sell ? "สีแดง" : "สีเขียว") : "ทิ้งหาง"}ในโซน` : b5 ? `แท่ง 5 นาทีล่าสุดปิด ${f(b5.c)} ยังไม่${sell ? "แตะโซนแล้วปิดแดง" : "แตะโซนแล้วปิดเขียว"} → รอ / ไป Next Station` : "รอแท่งปิด");
    const t = c.tape;
    if (!t || !side) set("flow", "fail", "ยังไม่มีข้อมูล tape");
    else { const okNet = sell ? t.net5 < 0 : t.net5 > 0, okPr = sell ? t.pressure < 50 : t.pressure > 50;
      set("flow", okNet && okPr ? "pass" : okNet || okPr ? "warn" : "fail", `Net 5 นาที ${t.net5 >= 0 ? "+" : "−"}${f(Math.abs(t.net5))} · Pressure ${t.pressure.toFixed(0)}% ซื้อ`); }
    // the order: entry at the trigger close (or the zone edge while waiting), SL beyond the zone and the trigger's tip
    let plan = null;
    if (z && side && v) {
      const entry = trig ? trig.bar.c : sell ? z.lo : z.hi, tip = trig ? (sell ? trig.bar.h : trig.bar.l) : (sell ? z.hi : z.lo);
      const stop = sell ? Math.max(z.hi, tip) + o.slBuf : Math.min(z.lo, tip) - o.slBuf, risk = Math.abs(entry - stop);
      const ahead = p => (p - entry) * dir > 0.5 * risk, walls = (c.walls || []).filter(w => w.persistent && w.side === (sell ? "bid" : "ask") && (w.price - entry) * dir > 0).sort((a, b) => (a.price - b.price) * dir);
      const pick = list => list.filter(x => x && ahead(x.p)).sort((a, b) => (a.p - b.p) * dir)[0] || null;
      const far = sell ? 0 : 1, edge2 = v.bands[2][far], edge3 = v.bands[3][far];
      const t1 = pick([{ p: v.vwap, name: "VWAP (เส้นกลาง Assist)" }, walls[0] && { p: walls[0].price, name: `กำแพงจริงแรก ${f(walls[0].size)}` }]) || { p: entry + dir * risk, name: "1R (ไม่มีแนว)" };
      const after = x => x && (x.p - t1.p) * dir > 0.5 * risk;
      const t2 = [{ p: edge2, name: `ขอบเมฆ${sell ? "ล่าง" : "บน"} (${sell ? "−" : "+"}2σ)` }, ...walls.slice(1).map(w => ({ p: w.price, name: `กำแพงจริง ${f(w.size)}` })), pr && { p: pr.poc, name: "Magnet (POC session ก่อน)" }]
        .filter(after).sort((a, b) => (a.p - b.p) * dir)[0] || { p: t1.p + dir * risk, name: "TP1 + 1R (ไม่มีแนว)" };
      const big = walls.filter(w => (w.price - t2.p) * dir > 0).sort((a, b) => b.size - a.size)[0];
      const t3 = big ? { p: big.price, name: `กำแพงใหญ่ฝั่งตรงข้าม ${f(big.size)}` } : { p: edge3, name: `เมฆ${sell ? "ล่าง" : "บน"} ${sell ? "−" : "+"}3σ` };
      plan = { side, entry, planned: !trig, stop, risk, tp1: t1.p, tp1Name: t1.name, tp2: t2.p, tp2Name: t2.name, tp3: (t3.p - t2.p) * dir > 0 ? t3.p : null, tp3Name: t3.name,
               zoneLo: z.lo, zoneHi: z.hi, names: z.names, trigger: trig ? { kind: trig.kind, t: trig.bar.t, end: (trig.bar.t + trig.sec) * 1000 } : null };
    }
    set("sltp", plan && (plan.tp1 - plan.entry) * dir > 0 && (plan.tp2 - plan.tp1) * dir > 0 ? "pass" : "fail",
      plan ? `SL ${f(plan.stop)} (เลยโซน ${o.slBuf} ดอลลาร์) · TP1 ${f(plan.tp1)} · TP2 ${f(plan.tp2)}` : "ยังวางไม่ได้");
    // hours (Bangkok): Europe ≈ 14:00–15:00, US ≈ 19:30–20:30 are the real moves; and no new entry around high-impact news
    const mins = Math.floor(now / 60000 + 420) % 1440, main = (mins >= 840 && mins < 900) ? "ยุโรปเปิด" : (mins >= 1170 && mins < 1230) ? "สหรัฐเปิด" : null;
    const n = c.news || { status: "unavailable" };
    set("news", n.status === "news" ? "fail" : main && n.status === "ok" ? "pass" : "warn",
      n.status === "news" ? `ช่วงข่าวแรง ${n.title || ""}: ไม่เปิดไม้ใหม่` : `${main ? `ช่วง${main} (จังหวะจริง)` : mins >= 360 && mins < 840 ? "เช้าเอเชีย flow เบา เน้นเฝ้าดู" : "นอกช่วงเปิดยุโรป/สหรัฐ"}${n.status === "unavailable" ? " · ปฏิทินข่าวยังไม่พร้อม" : " · ไม่มีข่าวแรงใน ±30 นาที"}`);
    const list = OF_ORDER.map(id => items[id]);
    const ready = !!plan && !plan.planned && list.filter(i => i.required).every(i => i.state === "pass" || (i.soft && i.state === "warn"));
    return { side, cvd, zone: z, vwap: v || null, shape, items: list, ready, plan };
  }

  return { PLAN, exact, split, delta, sessions, derive, atrDaily, atrOf, barIndexAt, oiRead, planLevels, cluster, biasModel, planModel, ofZone, checklist, OF_REQUIRED };
});
