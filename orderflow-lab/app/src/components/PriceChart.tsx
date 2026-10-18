// Price + volume with the desk's overlay layers on one time axis: VWAP with ±2σ/±3σ "Assist" bands,
// POC/value area, the previous session's range, round numbers, order-book walls as price lines,
// big trades as markers, and CVD / open-interest panes. Drag to pan, wheel/pinch to zoom
// (TradingView Lightweight Charts, Apache-2.0).
import { useEffect, useRef, useState } from "react";
import {
  BaselineSeries, CandlestickSeries, createChart, createSeriesMarkers, CrosshairMode, HistogramSeries, LineSeries, LineStyle,
  type IChartApi, type IPriceLine, type ISeriesApi, type ISeriesMarkersPluginApi, type SeriesMarker, type Time, type UTCTimestamp,
} from "lightweight-charts";
import type { Instrument, MarketDetails, Series } from "../data/types";
import { barDelta, cvd, niceStep, prevSession, roundLevels, stepOnBars, vwapBands } from "../domain/indicators";
import { BKK_OFFSET_S, fmtNum } from "../lib/format";

export interface Layers {
  vwap: boolean; assist: boolean; poc: boolean; range: boolean; round: boolean;
  wall: boolean; big: boolean; cvd: boolean; oi: boolean;
}
export const DEFAULT_LAYERS: Layers = { vwap: true, assist: false, poc: false, range: false, round: false, wall: true, big: true, cvd: true, oi: true };

interface Props {
  ins: Instrument;
  series: Series;
  layers: Layers;
  /** walls, big trades, profile and OI for the same instrument; null while loading or on error */
  details: MarketDetails | null;
  /** the view is re-fitted only when this changes (asset, timeframe, lookback), not on every refresh */
  viewKey: string;
  onCapture?: (capture: () => string | null) => void;
  /** what each layer could or could not draw, for the caption under the chart */
  onNotes?: (notes: Partial<Record<keyof Layers, string>>) => void;
}

const C = {
  buy: "#0b7a43", sell: "#b42318", vwap: "#2563eb", band: "rgba(37,99,235,0.55)", poc: "#8a5a00", range: "#566a7d",
  round: "#94a3b8", oi: "#7c3aed", grid: "#e7edf3", text: "#566a7d", line: "#d9e2ec",
};
const local = (t: number) => (t + BKK_OFFSET_S) as UTCTimestamp; // the axis shows Bangkok time (UTC+7)
const fmtV = (v: number) => (Math.abs(v) >= 1000 ? Math.round(v).toLocaleString("en-US") : Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2));
const LONG_WALL_S = 300; // a wall seen ≥ 5 minutes is drawn solid, newer ones dashed

type Api = {
  chart: IChartApi; candles: ISeriesApi<"Candlestick">; vol: ISeriesApi<"Histogram">; vw: ISeriesApi<"Line">;
  bands: ISeriesApi<"Line">[]; markers: ISeriesMarkersPluginApi<Time>;
  cvd: ISeriesApi<"Baseline"> | null; oi: ISeriesApi<"Line"> | null; panesKey: string; lines: IPriceLine[];
};

export function PriceChart({ ins, series, layers, details, viewKey, onCapture, onNotes }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const api = useRef<Api | null>(null);
  const [legend, setLegend] = useState<{ ohlc: string; flow: string; wall: string } | null>(null);
  const [drawn, setDrawn] = useState<Record<string, number>>({});
  const fitted = useRef("");

  useEffect(() => {
    if (!box.current) return;
    const chart = createChart(box.current, {
      autoSize: true,
      layout: { background: { color: "#ffffff" }, textColor: C.text, fontFamily: "'Noto Sans Thai', system-ui, sans-serif", fontSize: 12, attributionLogo: true,
        panes: { separatorColor: C.line, separatorHoverColor: "rgba(37,99,235,0.2)" } },
      grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
      rightPriceScale: { borderColor: C.line },
      timeScale: { borderColor: C.line, timeVisible: true, secondsVisible: false, rightOffset: 4 },
      crosshair: { mode: CrosshairMode.Normal },
      handleScroll: { vertTouchDrag: false },
    });
    const price = { type: "custom" as const, minMove: 1 / 10 ** ins.priceDecimals, formatter: (p: number) => fmtNum(p, ins.priceDecimals) };
    const candles = chart.addSeries(CandlestickSeries, { upColor: C.buy, downColor: "#ffffff", borderUpColor: C.buy, borderDownColor: C.sell, wickUpColor: C.buy, wickDownColor: C.sell, priceFormat: price });
    candles.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.22 } });
    const vol = chart.addSeries(HistogramSeries, { priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    const line = { lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false, priceFormat: price };
    const vw = chart.addSeries(LineSeries, { ...line, color: C.vwap, lineWidth: 2 });
    // Assist: +2σ, −2σ solid, +3σ, −3σ dashed (the desk's convention)
    const bands = [LineStyle.Solid, LineStyle.Solid, LineStyle.Dashed, LineStyle.Dashed]
      .map((s) => chart.addSeries(LineSeries, { ...line, color: C.band, lineWidth: 1, lineStyle: s }));
    const markers = createSeriesMarkers(candles, []);
    api.current = { chart, candles, vol, vw, bands, markers, cvd: null, oi: null, panesKey: "", lines: [] };
    onCapture?.(() => {
      try { return chart.takeScreenshot().toDataURL("image/jpeg", 0.82); } catch { return null; }
    });
    return () => { chart.remove(); api.current = null; };
  }, [ins.id, ins.priceDecimals]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const a = api.current;
    if (!a) return;
    const bars = series.bars;
    const barSec = bars.length > 1 ? Math.min(...bars.slice(1).map((b, i) => b.t - bars[i].t).filter((d) => d > 0)) : 60;
    const notes: Partial<Record<keyof Layers, string>> = {};
    const count: Record<string, number> = {};
    a.candles.setData(bars.map((b) => ({ time: local(b.t), open: b.o, high: b.h, low: b.l, close: b.c })));
    a.vol.setData(bars.map((b) => {
      const d = barDelta(b);
      return { time: local(b.t), value: b.v, color: d == null ? "rgba(86,106,125,0.35)" : d >= 0 ? "rgba(11,122,67,0.35)" : "rgba(180,35,24,0.35)" };
    }));

    // VWAP and its ±2σ / ±3σ bands
    const vb = vwapBands(bars, series.cvdAnchor.startT);
    const pts = (f: (p: { vwap: number; sd: number }) => number) => bars.map((b, i) => (vb[i] == null ? { time: local(b.t) } : { time: local(b.t), value: f(vb[i]!) }));
    a.vw.setData(layers.vwap ? pts((p) => p.vwap) : []);
    [(p: { vwap: number; sd: number }) => p.vwap + 2 * p.sd, (p: { vwap: number; sd: number }) => p.vwap - 2 * p.sd,
      (p: { vwap: number; sd: number }) => p.vwap + 3 * p.sd, (p: { vwap: number; sd: number }) => p.vwap - 3 * p.sd]
      .forEach((f, k) => a.bands[k].setData(layers.assist ? pts(f) : []));

    // lower panes: CVD first, then OI; rebuilt only when the set of panes changes
    const oiPts = details?.oi?.series ?? [];
    const wantOi = layers.oi && oiPts.length > 0;
    if (layers.oi && !oiPts.length) notes.oi = ins.market === "spot" ? "ตลาด spot ไม่มี Open Interest" : "ยังไม่มีข้อมูล OI";
    const key = `${layers.cvd}|${wantOi}`;
    if (key !== a.panesKey) {
      if (a.oi) { a.chart.removeSeries(a.oi); a.oi = null; }
      if (a.cvd) { a.chart.removeSeries(a.cvd); a.cvd = null; }
      for (let i = a.chart.panes().length - 1; i >= 1; i--) if (!a.chart.panes()[i].getSeries().length) a.chart.removePane(i);
      if (layers.cvd) {
        a.cvd = a.chart.addSeries(BaselineSeries, { baseValue: { type: "price", price: 0 }, lineWidth: 2, topLineColor: C.buy, bottomLineColor: C.sell,
          topFillColor1: "rgba(11,122,67,0.22)", topFillColor2: "rgba(11,122,67,0.03)", bottomFillColor1: "rgba(180,35,24,0.03)", bottomFillColor2: "rgba(180,35,24,0.22)",
          priceLineVisible: false, priceFormat: { type: "custom", minMove: 0.01, formatter: (v: number) => fmtNum(v, Math.abs(v) >= 100 ? 0 : 2) } }, 1);
        a.chart.panes()[1]?.setHeight(100);
      }
      if (wantOi) {
        const pane = layers.cvd ? 2 : 1;
        a.oi = a.chart.addSeries(LineSeries, { color: C.oi, lineWidth: 2, priceLineVisible: false,
          priceFormat: { type: "custom", minMove: 0.01, formatter: (v: number) => (Math.abs(v) >= 1e4 ? `${(v / 1e3).toFixed(1)}K` : fmtNum(v, 0)) } }, pane);
        a.chart.panes()[pane]?.setHeight(90);
      }
      a.panesKey = key;
    }
    if (a.cvd) {
      const c = cvd(bars, series.cvdAnchor.startT);
      a.cvd.setData(bars.map((b, i) => (c[i] == null ? { time: local(b.t) } : { time: local(b.t), value: c[i]! })));
    }
    if (a.oi) {
      const o = stepOnBars(bars, oiPts, barSec);
      a.oi.setData(bars.map((b, i) => (o[i] == null ? { time: local(b.t) } : { time: local(b.t), value: o[i]! })));
      count.oi = o.filter((v) => v != null).length;
    }

    // big trades: one marker per bar and side, below the bar = buyers, above = sellers, size by volume
    const mk: SeriesMarker<Time>[] = [];
    if (layers.big) {
      const agg = new Map<string, number>();
      for (const t of details?.bigTrades ?? []) {
        const s = Math.floor(t.at / 1000);
        const i = bars.findIndex((b) => s >= b.t && s < b.t + barSec);
        if (i < 0) continue;
        const k = `${bars[i].t}|${t.side}`;
        agg.set(k, (agg.get(k) ?? 0) + t.size);
      }
      const mx = Math.max(1e-9, ...agg.values());
      for (const [k, v] of agg) {
        const [t, side] = k.split("|");
        const buy = side === "buy";
        mk.push({ time: local(+t), position: buy ? "belowBar" : "aboveBar", shape: "circle", color: buy ? C.buy : C.sell,
          size: 0.6 + 1.6 * Math.sqrt(v / mx), text: v >= 0.4 * mx ? fmtV(v) : "" });
      }
      mk.sort((x, z) => (x.time as number) - (z.time as number));
      count.big = mk.length;
      if (!details) notes.big = "กำลังโหลดไม้ใหญ่";
      else if (!details.bigTrades.length) notes.big = "ยังไม่มีไม้ใหญ่ตามเกณฑ์ในช่วงนี้";
      else if (!mk.length) notes.big = "ไม้ใหญ่ที่มีอยู่นอกช่วงแท่งที่แสดง";
    }
    a.markers.setMarkers(mk);

    // levels as price lines; phones keep only axis labels so titles do not cover the candles
    for (const l of a.lines) a.candles.removePriceLine(l);
    a.lines = [];
    const narrow = (box.current?.clientWidth ?? 0) < 600;
    const pl = (price: number | null, title: string, color: string, lineStyle: LineStyle, lineWidth: 1 | 2 | 3 | 4, axis = true) => {
      if (price == null || !Number.isFinite(price)) return;
      a.lines.push(a.candles.createPriceLine({ price, color, lineStyle, lineWidth, title: narrow ? "" : title, axisLabelVisible: axis }));
    };
    const profile = details?.profile ?? null;
    if (layers.poc && profile?.poc != null) {
      pl(profile.poc, "POC", C.poc, LineStyle.Dashed, 2);
      pl(profile.vaHigh, "VA สูง", C.poc, LineStyle.Dotted, 1, false);
      pl(profile.vaLow, "VA ต่ำ", C.poc, LineStyle.Dotted, 1, false);
    } else if (layers.poc) notes.poc = details ? "ยังคำนวณ POC ไม่ได้" : "กำลังโหลด";
    if (layers.range && bars.length) {
      const bin = profile?.binSize ?? niceStep(bars[bars.length - 1].c / 5000);
      const r = prevSession(bars, bin);
      if ("reason" in r) notes.range = r.reason;
      else {
        pl(r.hi, "Y-Hi", C.sell, LineStyle.Solid, 1);
        pl(r.vaHigh, "Top", C.range, LineStyle.Dashed, 1);
        pl(r.poc, "Y-POC", C.poc, LineStyle.Dashed, 2);
        pl(r.vaLow, "Bottom", C.range, LineStyle.Dashed, 1);
        pl(r.lo, "Y-Lo", C.buy, LineStyle.Solid, 1);
        count.range = 5;
      }
    }
    if (layers.round && bars.length) {
      const recent = bars.slice(-400);
      const { levels } = roundLevels(Math.min(...recent.map((b) => b.l)), Math.max(...recent.map((b) => b.h)));
      for (const v of levels) pl(v, "", C.round, LineStyle.SparseDotted, 1, false);
      count.round = levels.length;
    }
    if (layers.wall) {
      const act = (details?.walls ?? []).slice(0, 8);
      const mx = Math.max(1e-9, ...act.map((w) => w.size));
      for (const w of act) {
        pl(w.price, `${w.side === "bid" ? "Bid" : "Ask"} ${fmtV(w.size)}`, w.side === "bid" ? C.buy : C.sell,
          w.ageSec >= LONG_WALL_S ? LineStyle.Solid : LineStyle.Dashed, w.size >= 0.6 * mx ? 3 : w.size >= 0.3 * mx ? 2 : 1);
      }
      count.wall = act.length;
      if (!details) notes.wall = "กำลังโหลดกำแพง";
      else if (!act.length) notes.wall = details.quality.find((q) => q.stream === "book")?.status === "ok"
        ? "ตอนนี้ไม่มีกำแพงตามเกณฑ์" : "order book ใช้ไม่ได้ตอนนี้ จึงไม่แสดงกำแพง";
    }
    setDrawn(count);
    onNotes?.(notes);

    if (fitted.current !== viewKey && bars.length) {
      const n = bars.length;
      a.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - (box.current!.clientWidth < 600 ? 70 : 150)), to: n + 4 });
      fitted.current = viewKey;
    }
    const walls = layers.wall ? details?.walls ?? [] : [];
    const show = (i: number, y?: number) => {
      const b = bars[i];
      if (!b) return setLegend(null);
      const d = barDelta(b);
      const D = ins.priceDecimals;
      const near = y == null ? undefined : walls.filter((w) => Math.abs(w.price - y) <= Math.max(b.h - b.l, b.c * 0.0005)).sort((x, z) => Math.abs(x.price - y) - Math.abs(z.price - y))[0];
      setLegend({
        ohlc: `O ${fmtNum(b.o, D)}  H ${fmtNum(b.h, D)}  L ${fmtNum(b.l, D)}  C ${fmtNum(b.c, D)}`,
        flow: `V ${fmtV(b.v)} ${ins.qtyUnit}` + (b.buyV == null || d == null ? " · ไม่ทราบฝั่งผู้กด"
          : ` · ซื้อ ${fmtV(b.buyV)} · ขาย ${fmtV(b.v - b.buyV)} · Δ ${d >= 0 ? "+" : "−"}${fmtV(Math.abs(d))}`),
        wall: near ? `กำแพง ${near.side === "bid" ? "BID" : "ASK"} ${fmtNum(near.price, D)} · ${fmtV(near.size)} ${ins.qtyUnit}` : "",
      });
    };
    show(bars.length - 1);
    const onMove = (p: { time?: unknown; point?: { y: number } }) => {
      if (p.time == null) return show(bars.length - 1);
      const t = (p.time as number) - BKK_OFFSET_S;
      const i = bars.findIndex((b) => b.t === t);
      const y = p.point ? a.candles.coordinateToPrice(p.point.y) : null;
      show(i >= 0 ? i : bars.length - 1, y ?? undefined);
    };
    a.chart.subscribeCrosshairMove(onMove);
    return () => a.chart.unsubscribeCrosshairMove(onMove);
  }, [series, layers, details, ins, viewKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const toLatest = () => api.current?.chart.timeScale().scrollToRealTime();
  const reset = () => {
    const n = series.bars.length;
    api.current?.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - ((box.current?.clientWidth ?? 0) < 600 ? 70 : 150)), to: n + 4 });
  };
  const last = series.bars[series.bars.length - 1];
  const hi = Math.max(...series.bars.map((b) => b.h));
  const lo = Math.min(...series.bars.map((b) => b.l));
  const summary = last ? `กราฟแท่งเทียน ${ins.symbol} ${series.bars.length} แท่ง ราคาปิดล่าสุด ${fmtNum(last.c, ins.priceDecimals)} ${ins.quote} สูงสุดในช่วง ${fmtNum(hi, ins.priceDecimals)} ต่ำสุด ${fmtNum(lo, ins.priceDecimals)}`
    + (drawn.wall ? ` · กำแพง ${drawn.wall} ระดับ` : "") + (drawn.big ? ` · ไม้ใหญ่ ${drawn.big} จุด` : "") : "ยังไม่มีข้อมูลกราฟ";
  return (
    <>
      <div className="chart-tools">
        <button type="button" className="btn small" onClick={toLatest}>⇥ แท่งล่าสุด</button>
        <button type="button" className="btn small" onClick={reset}>รีเซ็ตมุมมอง</button>
      </div>
      <div className="chart-box" ref={box} role="img" aria-label={summary}
        data-walls={drawn.wall ?? 0} data-big={drawn.big ?? 0} data-oi={drawn.oi ?? 0} data-round={drawn.round ?? 0} data-range={drawn.range ?? 0}>
        {legend && <div className="chart-legend"><span>{legend.ohlc}</span><span className="flow">{legend.flow}</span>{legend.wall && <span className="flow">{legend.wall}</span>}</div>}
      </div>
    </>
  );
}
