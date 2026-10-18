// Price + volume with an optional VWAP line, POC/value-area lines and a CVD pane on the same
// time axis. Drag to pan, wheel/pinch to zoom (TradingView Lightweight Charts, Apache-2.0).
import { useEffect, useRef, useState } from "react";
import {
  BaselineSeries, CandlestickSeries, createChart, CrosshairMode, HistogramSeries, LineSeries, LineStyle,
  type IChartApi, type IPriceLine, type ISeriesApi, type UTCTimestamp,
} from "lightweight-charts";
import type { Instrument, Series } from "../data/types";
import { barDelta, cvd, vwap } from "../domain/indicators";
import { BKK_OFFSET_S, fmtNum } from "../lib/format";

export interface Layers { vwap: boolean; poc: boolean; cvd: boolean }

interface Props {
  ins: Instrument;
  series: Series;
  layers: Layers;
  profile: { poc: number | null; vaLow: number | null; vaHigh: number | null } | null;
  /** the view is re-fitted only when this changes (asset, timeframe, lookback), not on every refresh */
  viewKey: string;
  onCapture?: (capture: () => string | null) => void;
}

const C = { buy: "#0b7a43", sell: "#b42318", vwap: "#2563eb", poc: "#8a5a00", grid: "#e7edf3", text: "#566a7d", line: "#d9e2ec" };
const local = (t: number) => (t + BKK_OFFSET_S) as UTCTimestamp; // the axis shows Bangkok time (UTC+7)

export function PriceChart({ ins, series, layers, profile, viewKey, onCapture }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const api = useRef<{ chart: IChartApi; candles: ISeriesApi<"Candlestick">; vol: ISeriesApi<"Histogram">; vw: ISeriesApi<"Line">; cvd: ISeriesApi<"Baseline"> | null; lines: IPriceLine[] } | null>(null);
  const [legend, setLegend] = useState<{ ohlv: string; c: string } | null>(null);
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
    const vw = chart.addSeries(LineSeries, { color: C.vwap, lineWidth: 2, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false, priceFormat: price });
    api.current = { chart, candles, vol, vw, cvd: null, lines: [] };
    onCapture?.(() => {
      try { return chart.takeScreenshot().toDataURL("image/jpeg", 0.82); } catch { return null; }
    });
    return () => { chart.remove(); api.current = null; };
  }, [ins.id, ins.priceDecimals]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const a = api.current;
    if (!a) return;
    const bars = series.bars;
    a.candles.setData(bars.map((b) => ({ time: local(b.t), open: b.o, high: b.h, low: b.l, close: b.c })));
    a.vol.setData(bars.map((b) => {
      const d = barDelta(b);
      return { time: local(b.t), value: b.v, color: d == null ? "rgba(86,106,125,0.35)" : d >= 0 ? "rgba(11,122,67,0.35)" : "rgba(180,35,24,0.35)" };
    }));
    const vw = vwap(bars, series.cvdAnchor.startT);
    a.vw.setData(layers.vwap ? bars.map((b, i) => (vw[i] == null ? { time: local(b.t) } : { time: local(b.t), value: vw[i]! })) : []);
    // CVD pane
    if (layers.cvd && !a.cvd) {
      a.cvd = a.chart.addSeries(BaselineSeries, { baseValue: { type: "price", price: 0 }, lineWidth: 2, topLineColor: C.buy, bottomLineColor: C.sell,
        topFillColor1: "rgba(11,122,67,0.22)", topFillColor2: "rgba(11,122,67,0.03)", bottomFillColor1: "rgba(180,35,24,0.03)", bottomFillColor2: "rgba(180,35,24,0.22)",
        priceLineVisible: false, priceFormat: { type: "custom", minMove: 0.01, formatter: (v: number) => fmtNum(v, Math.abs(v) >= 100 ? 0 : 2) } }, 1);
      a.chart.panes()[1]?.setHeight(110);
    } else if (!layers.cvd && a.cvd) {
      a.chart.removeSeries(a.cvd);
      a.cvd = null;
    }
    if (a.cvd) {
      const c = cvd(bars, series.cvdAnchor.startT);
      a.cvd.setData(bars.map((b, i) => (c[i] == null ? { time: local(b.t) } : { time: local(b.t), value: c[i]! })));
    }
    for (const l of a.lines) a.candles.removePriceLine(l);
    a.lines = [];
    if (layers.poc && profile?.poc != null) {
      a.lines.push(a.candles.createPriceLine({ price: profile.poc, color: C.poc, lineWidth: 2, lineStyle: LineStyle.Dashed, title: "POC", axisLabelVisible: true }));
      if (profile.vaHigh != null) a.lines.push(a.candles.createPriceLine({ price: profile.vaHigh, color: C.poc, lineWidth: 1, lineStyle: LineStyle.Dotted, title: "VA สูง", axisLabelVisible: false }));
      if (profile.vaLow != null) a.lines.push(a.candles.createPriceLine({ price: profile.vaLow, color: C.poc, lineWidth: 1, lineStyle: LineStyle.Dotted, title: "VA ต่ำ", axisLabelVisible: false }));
    }
    if (fitted.current !== viewKey && bars.length) {
      const n = bars.length;
      a.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - (box.current!.clientWidth < 600 ? 70 : 150)), to: n + 4 });
      fitted.current = viewKey;
    }
    const show = (i: number) => {
      const b = bars[i];
      if (!b) return setLegend(null);
      const d = barDelta(b);
      // phones show close and delta only, so the legend does not cover the candles
      setLegend({ ohlv: `O ${fmtNum(b.o, ins.priceDecimals)}  H ${fmtNum(b.h, ins.priceDecimals)}  L ${fmtNum(b.l, ins.priceDecimals)} · V ${fmtNum(b.v, 1)} ${ins.qtyUnit} · `,
        c: `C ${fmtNum(b.c, ins.priceDecimals)}` + (d == null ? " · Δ ไม่ทราบ" : ` · Δ ${d >= 0 ? "+" : "−"}${fmtNum(Math.abs(d), 1)}`) });
    };
    show(bars.length - 1);
    const onMove = (p: { time?: unknown }) => {
      if (p.time == null) return show(bars.length - 1);
      const t = (p.time as number) - BKK_OFFSET_S;
      const i = bars.findIndex((b) => b.t === t);
      show(i >= 0 ? i : bars.length - 1);
    };
    a.chart.subscribeCrosshairMove(onMove);
    return () => a.chart.unsubscribeCrosshairMove(onMove);
  }, [series, layers, profile, ins, viewKey]);

  const last = series.bars[series.bars.length - 1];
  const hi = Math.max(...series.bars.map((b) => b.h));
  const lo = Math.min(...series.bars.map((b) => b.l));
  const summary = last ? `กราฟแท่งเทียน ${ins.symbol} ${series.bars.length} แท่ง ราคาปิดล่าสุด ${fmtNum(last.c, ins.priceDecimals)} ${ins.quote} สูงสุดในช่วง ${fmtNum(hi, ins.priceDecimals)} ต่ำสุด ${fmtNum(lo, ins.priceDecimals)}` : "ยังไม่มีข้อมูลกราฟ";
  return (
    <div className="chart-box" ref={box} role="img" aria-label={summary}>
      {legend && <div className="chart-legend"><span className="ohlv">{legend.ohlv}</span>{legend.c}</div>}
    </div>
  );
}
