// Run: node --test order-flow/engine.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const E = require('./engine.js');

const close = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) <= eps, `${a} !~ ${b}`);

test('zScores scores each value against the preceding n only', () => {
  const v = [1, 2, 3, 4, 10];
  const z = E.zScores(v, 4);
  assert.deepEqual(z.slice(0, 4), [null, null, null, null]);
  // prior window [1,2,3,4]: mean 2.5, population sd sqrt(1.25)
  close(z[4], (10 - 2.5) / Math.sqrt(1.25));
});

test('zScores is null when the prior window has no spread', () => {
  assert.equal(E.zScores([5, 5, 5, 9], 3)[3], null);
});

test('realizedVol annualizes constant log returns', () => {
  const r = 0.001;
  const closes = [100];
  for (let i = 0; i < 30; i++) closes.push(closes[i] * Math.exp(r));
  const barsPerYear = (365 * 86400) / 60;
  close(E.realizedVol(closes, 60, 365), r * Math.sqrt(barsPerYear), 1e-12);
});

test('rollingRealizedVol leaves the warm-up window empty', () => {
  const out = E.rollingRealizedVol([1, 2, 3, 4, 5], 3, 60);
  assert.deepEqual(out.slice(0, 3), [null, null, null]);
  assert.ok(out[3] > 0 && out[4] > 0);
});

test('parkinsonVol matches the closed form for a constant range', () => {
  const k = Math.log(1.01);
  const expected = Math.sqrt((k * k) / (4 * Math.LN2)) * Math.sqrt(E.barsPerYear(60, 365));
  close(E.parkinsonVol([101, 101], [100, 100], 60, 365), expected, 1e-12);
});

test('atr uses true range and Wilder smoothing', () => {
  const c = [{ h: 10, l: 9, c: 9.5 }];
  for (let i = 0; i < 14; i++) c.push({ h: 11, l: 10, c: 10.5 });
  // first TR includes the gap from the previous close (11 - 9.5 = 1.5), the rest are 1
  close(E.atr(c, 14), (1.5 + 13) / 14);
  assert.ok(Number.isNaN(E.atr(c.slice(0, 5), 14)));
});

test('percentileRank, quantile and volRegime', () => {
  const s = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
  assert.equal(E.percentileRank(s, 3), 30);
  close(E.quantile(s, 0.5), 5.5);
  assert.equal(E.volRegime(10), 'compressed');
  assert.equal(E.volRegime(50), 'normal');
  assert.equal(E.volRegime(80), 'elevated');
  assert.equal(E.volRegime(95), 'extreme');
});

test('bookImbalance compares resting size on each side', () => {
  const bids = [[100, 3], [99, 1]];
  const asks = [[101, 1], [102, 1]];
  close(E.bookImbalance(bids, asks, 1), (3 - 1) / 4);
  close(E.bookImbalance(bids, asks, 2), (4 - 2) / 6);
  assert.ok(Number.isNaN(E.bookImbalance([], [], 5)));
});

test('valueArea expands from the POC toward the heavier side', () => {
  const rows = [
    { price: 1, vol: 1 },
    { price: 2, vol: 5 },
    { price: 3, vol: 10 },
    { price: 4, vol: 3 },
    { price: 5, vol: 1 },
  ];
  const va = E.valueArea(rows, 0.7);
  assert.equal(va.poc, 3);
  assert.equal(va.total, 20);
  // 10 -> +5 (price 2) = 15 >= 14
  assert.equal(va.val, 2);
  assert.equal(va.vah, 3);
});

test('vwap and its volume-weighted sd', () => {
  const acc = E.createVwap();
  E.vwapAdd(acc, 10, 1);
  E.vwapAdd(acc, 20, 3);
  const { vwap, sd } = E.vwapValue(acc);
  close(vwap, 17.5);
  close(sd, Math.sqrt((1 * 7.5 ** 2 + 3 * 2.5 ** 2) / 4));
  assert.ok(Number.isNaN(E.vwapValue(E.createVwap()).vwap));
});

test('resample aggregates and fills quiet intervals with zero-volume bars', () => {
  const bars = [
    { t: 0, o: 1, h: 2, l: 1, c: 2, buy: 1, sell: 0, n: 1 },
    { t: 1, o: 2, h: 3, l: 2, c: 3, buy: 0, sell: 2, n: 1 },
    { t: 12, o: 3, h: 3, l: 1, c: 1, buy: 4, sell: 0, n: 1 },
  ];
  const out = E.resample(bars, 5, 15);
  assert.deepEqual(out.map((b) => b.t), [0, 5, 10, 15]);
  assert.deepEqual([out[0].o, out[0].h, out[0].l, out[0].c, out[0].buy, out[0].sell], [1, 3, 1, 3, 1, 2]);
  assert.deepEqual([out[1].o, out[1].c, out[1].buy, out[1].sell], [3, 3, 0, 0]);
  assert.equal(out[3].c, 1);
  assert.equal(E.resample(bars, 5).length, 2);
});

test('parseKlines splits volume into taker buy and sell', () => {
  const [b] = E.parseKlines([[60000, '1', '2', '0.5', '1.5', '10', 119999, '0', 7, '6', '0', '0']]);
  assert.deepEqual(b, { t: 60, o: 1, h: 2, l: 0.5, c: 1.5, buy: 6, sell: 4, n: 7 });
});

test('cvdDivergence flags price/CVD disagreement', () => {
  const up = [1, 2, 3, 4, 5, 6];
  const cvdDown = [5, 6, 4, 3, 2, 1];
  assert.equal(E.cvdDivergence(up, cvdDown, 6), 'bearish');
  const down = [6, 5, 4, 3, 2, 1];
  const cvdUp = [0, -1, 1, 2, 3, 4];
  assert.equal(E.cvdDivergence(down, cvdUp, 6), 'bullish');
  assert.equal(E.cvdDivergence(up, up, 6), null);
  assert.equal(E.cvdDivergence(up, cvdDown, 10), null);
});

test('niceStep rounds to 1-2-5 steps', () => {
  close(E.niceStep(0.826), 1);
  close(E.niceStep(20.6), 20);
  close(E.niceStep(0.6), 0.5);
  close(E.niceStep(0.0106), 0.01);
});
