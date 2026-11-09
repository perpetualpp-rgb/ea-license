/*
 * Order-flow analytics — pure functions, no DOM, no network.
 * Loaded in the browser as window.OFEngine and in Node via require() for tests.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.OFEngine = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const SECONDS_PER_DAY = 86400;

  function mean(xs) {
    if (!xs.length) return NaN;
    let s = 0;
    for (const x of xs) s += x;
    return s / xs.length;
  }

  // Population standard deviation.
  function stdev(xs, m) {
    if (xs.length < 2) return NaN;
    if (m === undefined) m = mean(xs);
    let s = 0;
    for (const x of xs) s += (x - m) * (x - m);
    return Math.sqrt(s / xs.length);
  }

  // z_t = (V_t - mean(V_{t-n..t-1})) / std(V_{t-n..t-1}).
  // null until n prior values exist, or when the prior window has no spread.
  function zScores(values, n) {
    const out = new Array(values.length).fill(null);
    for (let i = n; i < values.length; i++) {
      const win = values.slice(i - n, i);
      const m = mean(win);
      const sd = stdev(win, m);
      out[i] = sd > 0 ? (values[i] - m) / sd : null;
    }
    return out;
  }

  function barsPerYear(barSeconds, daysPerYear) {
    return (daysPerYear * SECONDS_PER_DAY) / barSeconds;
  }

  function logReturns(closes) {
    const r = [];
    for (let i = 1; i < closes.length; i++) {
      if (closes[i] > 0 && closes[i - 1] > 0) r.push(Math.log(closes[i] / closes[i - 1]));
    }
    return r;
  }

  // Close-to-close realized volatility, annualized: sqrt(mean(r^2)) * sqrt(bars per year).
  function realizedVol(closes, barSeconds, daysPerYear = 365) {
    const r = logReturns(closes);
    if (!r.length) return NaN;
    let s = 0;
    for (const x of r) s += x * x;
    return Math.sqrt(s / r.length) * Math.sqrt(barsPerYear(barSeconds, daysPerYear));
  }

  // Rolling realized vol over `window` returns (window + 1 closes).
  function rollingRealizedVol(closes, window, barSeconds, daysPerYear = 365) {
    const out = new Array(closes.length).fill(null);
    for (let i = window; i < closes.length; i++) {
      out[i] = realizedVol(closes.slice(i - window, i + 1), barSeconds, daysPerYear);
    }
    return out;
  }

  // Parkinson high-low estimator: sigma^2 = mean(ln(H/L)^2) / (4 ln 2), annualized.
  function parkinsonVol(highs, lows, barSeconds, daysPerYear = 365) {
    let s = 0;
    let n = 0;
    for (let i = 0; i < highs.length; i++) {
      if (highs[i] > 0 && lows[i] > 0) {
        const x = Math.log(highs[i] / lows[i]);
        s += x * x;
        n++;
      }
    }
    if (!n) return NaN;
    return Math.sqrt(s / n / (4 * Math.LN2)) * Math.sqrt(barsPerYear(barSeconds, daysPerYear));
  }

  // Wilder's ATR. candles: [{h, l, c}], oldest first.
  function atr(candles, n = 14) {
    if (candles.length < n + 1) return NaN;
    const tr = [];
    for (let i = 1; i < candles.length; i++) {
      const { h, l } = candles[i];
      const pc = candles[i - 1].c;
      tr.push(Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc)));
    }
    let a = mean(tr.slice(0, n));
    for (let i = n; i < tr.length; i++) a = (a * (n - 1) + tr[i]) / n;
    return a;
  }

  // Share of the sample that is <= v, in percent.
  function percentileRank(sample, v) {
    if (!Number.isFinite(v)) return NaN;
    let n = 0;
    let k = 0;
    for (const x of sample) {
      if (!Number.isFinite(x)) continue;
      n++;
      if (x <= v) k++;
    }
    return n ? (100 * k) / n : NaN;
  }

  // Linear-interpolated quantile, q in [0, 1].
  function quantile(sample, q) {
    const xs = sample.filter(Number.isFinite).sort((a, b) => a - b);
    if (!xs.length) return NaN;
    const pos = (xs.length - 1) * q;
    const lo = Math.floor(pos);
    const hi = Math.ceil(pos);
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo);
  }

  function volRegime(pct) {
    if (!Number.isFinite(pct)) return 'n/a';
    if (pct < 20) return 'compressed';
    if (pct < 70) return 'normal';
    if (pct < 90) return 'elevated';
    return 'extreme';
  }

  // Order-book imbalance over the top `levels` of each side, in [-1, 1].
  // bids/asks: [[price, size], ...], best level first.
  function bookImbalance(bids, asks, levels) {
    let b = 0;
    let a = 0;
    for (let i = 0; i < Math.min(levels, bids.length); i++) b += bids[i][1];
    for (let i = 0; i < Math.min(levels, asks.length); i++) a += asks[i][1];
    return b + a > 0 ? (b - a) / (b + a) : NaN;
  }

  // Point of control and value area. rows: [{price, vol}] sorted by price ascending.
  // Expands from the POC one level at a time toward the side with more volume.
  function valueArea(rows, pct = 0.7) {
    if (!rows.length) return null;
    let total = 0;
    let poc = 0;
    rows.forEach((r, i) => {
      total += r.vol;
      if (r.vol > rows[poc].vol) poc = i;
    });
    let lo = poc;
    let hi = poc;
    let acc = rows[poc].vol;
    while (acc < total * pct && (lo > 0 || hi < rows.length - 1)) {
      const up = hi < rows.length - 1 ? rows[hi + 1].vol : -1;
      const dn = lo > 0 ? rows[lo - 1].vol : -1;
      if (up >= dn) acc += rows[++hi].vol;
      else acc += rows[--lo].vol;
    }
    return { poc: rows[poc].price, val: rows[lo].price, vah: rows[hi].price, total };
  }

  // Running volume-weighted average price with a volume-weighted standard deviation.
  function createVwap() {
    return { pv: 0, v: 0, p2v: 0 };
  }
  function vwapAdd(acc, price, vol) {
    acc.pv += price * vol;
    acc.v += vol;
    acc.p2v += price * price * vol;
  }
  function vwapValue(acc) {
    if (!(acc.v > 0)) return { vwap: NaN, sd: NaN };
    const vwap = acc.pv / acc.v;
    return { vwap, sd: Math.sqrt(Math.max(0, acc.p2v / acc.v - vwap * vwap)) };
  }

  function emptyBar(t, price) {
    return { t, o: price, h: price, l: price, c: price, buy: 0, sell: 0, n: 0 };
  }

  // Aggregate bars ({t: start second, o, h, l, c, buy, sell, n}, oldest first) into `size`-second bars.
  // With fillUntil (a bar start time), missing intervals become flat zero-volume bars, so
  // quiet periods count as zero volume instead of disappearing.
  function resample(bars, size, fillUntil) {
    const out = [];
    for (const b of bars) {
      const t = Math.floor(b.t / size) * size;
      let cur = out[out.length - 1];
      if (!cur || t > cur.t) {
        if (cur && fillUntil !== undefined) {
          for (let ft = cur.t + size; ft < t; ft += size) out.push(emptyBar(ft, cur.c));
        }
        cur = { t, o: b.o, h: b.h, l: b.l, c: b.c, buy: 0, sell: 0, n: 0 };
        out.push(cur);
      } else {
        if (b.h > cur.h) cur.h = b.h;
        if (b.l < cur.l) cur.l = b.l;
        cur.c = b.c;
      }
      cur.buy += b.buy;
      cur.sell += b.sell;
      cur.n += b.n;
    }
    if (fillUntil !== undefined && out.length) {
      const last = out[out.length - 1];
      for (let ft = last.t + size; ft <= fillUntil; ft += size) out.push(emptyBar(ft, last.c));
    }
    return out;
  }

  // Binance kline rows -> bars. Taker-buy base volume is aggressive buying; the rest is aggressive selling.
  function parseKlines(rows) {
    return rows.map((k) => {
      const vol = +k[5];
      const buy = +k[9];
      return {
        t: Math.floor(k[0] / 1000),
        o: +k[1],
        h: +k[2],
        l: +k[3],
        c: +k[4],
        buy,
        sell: Math.max(0, vol - buy),
        n: +k[8],
      };
    });
  }

  // Price vs CVD divergence over the last `lookback` points, comparing the newer half with the older half.
  // 'bearish': price makes a higher high while CVD makes a lower high.
  // 'bullish': price makes a lower low while CVD makes a higher low.
  function cvdDivergence(closes, cvd, lookback = 30) {
    const n = Math.min(closes.length, cvd.length);
    if (n < lookback || lookback < 4) return null;
    const half = Math.floor(lookback / 2);
    const p1 = closes.slice(n - lookback, n - half);
    const p2 = closes.slice(n - half, n);
    const c1 = cvd.slice(n - lookback, n - half);
    const c2 = cvd.slice(n - half, n);
    if (Math.max(...p2) > Math.max(...p1) && Math.max(...c2) < Math.max(...c1)) return 'bearish';
    if (Math.min(...p2) < Math.min(...p1) && Math.min(...c2) > Math.min(...c1)) return 'bullish';
    return null;
  }

  // 1, 2, 5 x 10^k step closest to x (used for price buckets and axis ticks).
  function niceStep(x) {
    if (!(x > 0)) return 1;
    const e = Math.pow(10, Math.floor(Math.log10(x)));
    const f = x / e;
    const m = f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10;
    return m * e;
  }

  return {
    mean,
    stdev,
    zScores,
    barsPerYear,
    logReturns,
    realizedVol,
    rollingRealizedVol,
    parkinsonVol,
    atr,
    percentileRank,
    quantile,
    volRegime,
    bookImbalance,
    valueArea,
    createVwap,
    vwapAdd,
    vwapValue,
    resample,
    parseKlines,
    cvdDivergence,
    niceStep,
  };
});
