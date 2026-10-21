// Price + volume with the desk's overlay layers on one time axis: VWAP with ±2σ/±3σ "Assist" bands,
// POC/value area, the previous session's range, round numbers, order-book walls as price lines,
// big trades as markers, and CVD / open-interest panes. Drag to pan, wheel/pinch to zoom
// (TradingView Lightweight Charts, Apache-2.0).
import { useEffect, useRef, useState, type ReactNode } from "react";
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
  /** controls shown at the top of the full-screen chart (timeframe, layers) */
  fullHeader?: ReactNode;
  /** when the data on screen was last fetched, shown so a still chart is not mistaken for a frozen one */
  updatedAt?: number | null;
  /** phones show the freshness label here instead of a badge above the chart */
  freshness?: string;
}

/** OI value with its 1-hour change for the readout line, so OI is visible without finding the pane */
function oiText(d: MarketDetails | null, layers: Layers, market: string): string {
  if (!layers.oi) return "";
  if (market === "spot") return "OI: ไม่มีในตลาด spot (เลือก Perp)";
  const oi = d?.oi;
  if (!oi || oi.value == null) return "OI: ยังไม่มีข้อมูล";
  const v = Math.abs(oi.value) >= 1e4 ? `${(oi.value / 1e3).toFixed(1)}K` : fmtNum(oi.value, 0);
  const ch = oi.change1hPct == null ? "" : ` · 1 ชม. ${oi.change1hPct >= 0 ? "+" : "−"}${fmtNum(Math.abs(oi.change1hPct), 2)}%`;
  return `OI ${v} ${oi.unit}${ch} (แผงม่วงล่างสุด)`;
}

const C = {
  buy: "#0b7a43", sell: "#b42318", vwap: "#2563eb", band: "rgba(37,99,235,0.55)", poc: "#8a5a00", range: "#566a7d",
  round: "#94a3b8", oi: "#7c3aed", grid: "#e7edf3", text: "#566a7d", line: "#d9e2ec",
};
const local = (t: number) => (t + BKK_OFFSET_S) as UTCTimestamp; // the axis shows Bangkok time (UTC+7)
const fmtV = (v: number) => (Math.abs(v) >= 1000 ? Math.round(v).toLocaleString("en-US") : Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2));
const LONG_WALL_S = 300; // a wall seen ≥ 5 minutes is drawn solid, newer ones dashed
const narrowNow = () => typeof window !== "undefined" && window.innerWidth < 600;

type Api = {
  chart: IChartApi; candles: ISeriesApi<"Candlestick">; vol: ISeriesApi<"Histogram">; vw: ISeriesApi<"Line">;
  bands: ISeriesApi<"Line">[]; markers: ISeriesMarkersPluginApi<Time>;
  cvd: ISeriesApi<"Baseline"> | null; oi: ISeriesApi<"Line"> | null; panesKey: string; lines: IPriceLine[];
};

export function PriceChart({ ins, series, layers, details, viewKey, onCapture, onNotes, fullHeader, updatedAt, freshness }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const api = useRef<Api | null>(null);
  const [legend, setLegend] = useState<{ ohlc: string; flow: string; wall: string } | null>(null);
  const [drawn, setDrawn] = useState<Record<string, number>>({});
  const [full, setFull] = useState(false);
  const fitted = useRef("");

  // full screen: the page behind stops scrolling, so every drag belongs to the chart (TradingView-like);
  // Escape or ✕ closes it
  useEffect(() => {
    api.current?.chart.applyOptions({ handleScroll: { vertTouchDrag: full } });
    if (!full) return;
    // claim every touch that starts on the chart: preventDefault on a non-passive touchstart tells the
    // browser (and a WebView inside another app) that the page handles this gesture itself;
    // the chart library still receives the events and pans / zooms
    const el = box.current;
    const claim = (e: TouchEvent) => { if (e.cancelable) e.preventDefault(); };
    el?.addEventListener("touchstart", claim, { passive: false });
    el?.addEventListener("touchmove", claim, { passive: false });
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setFull(false); };
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = prev;
      window.removeEventListener("keydown", onKey);
      el?.removeEventListener("touchstart", claim);
      el?.removeEventListener("touchmove", claim);
    };
  }, [full]);

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
    // the visible range is mirrored on the element so tests (and screen readers' summary) can follow pans and zooms
    chart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
      if (r && box.current) { box.current.dataset.from = r.from.toFixed(1); box.current.dataset.to = r.to.toFixed(1); }
    });
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
          title: "CVD", priceLineVisible: false, priceFormat: { type: "custom", minMove: 0.01, formatter: (v: number) => fmtNum(v, Math.abs(v) >= 100 ? 0 : 2) } }, 1);
        a.chart.panes()[1]?.setHeight(narrowNow() ? 64 : 100);
      }
      if (wantOi) {
        const pane = layers.cvd ? 2 : 1;
        // the pane names itself on the price axis ("OI 132.6K"), so it is not just an unlabelled purple line
        a.oi = a.chart.addSeries(LineSeries, { color: C.oi, lineWidth: 2, priceLineVisible: false, title: "OI",
          priceFormat: { type: "custom", minMove: 0.01, formatter: (v: number) => (Math.abs(v) >= 1e4 ? `${(v / 1e3).toFixed(1)}K` : fmtNum(v, 0)) } }, pane);
        a.chart.panes()[pane]?.setHeight(narrowNow() ? 56 : 90);
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
          size: narrowNow() ? 0.4 + 0.9 * Math.sqrt(v / mx) : 0.6 + 1.6 * Math.sqrt(v / mx), // phones: smaller dots without numbers
          text: !narrowNow() && v >= 0.4 * mx ? fmtV(v) : "" });
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
    const narrow = narrowNow();
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
      const act = (details?.walls ?? []).slice(0, narrow ? 5 : 8); // phones: the five largest, so labels stay readable
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
      a.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - (narrowNow() ? 60 : 150)), to: n + 4 });
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

  // buttons do what the gestures do, for hosts that keep touch gestures for themselves
  const ts = () => api.current?.chart.timeScale();
  const pan = (dir: -1 | 1) => {
    const r = ts()?.getVisibleLogicalRange();
    if (!r) return;
    const step = (r.to - r.from) * 0.3 * dir;
    ts()!.setVisibleLogicalRange({ from: r.from + step, to: r.to + step });
  };
  const zoom = (k: number) => {
    const r = ts()?.getVisibleLogicalRange();
    if (!r) return;
    const w = Math.max(10, Math.min(series.bars.length + 20, (r.to - r.from) * k));
    ts()!.setVisibleLogicalRange({ from: r.to - w, to: r.to });
  };
  const toLatest = () => ts()?.scrollToRealTime();
  const reset = () => {
    const n = series.bars.length;
    ts()?.setVisibleLogicalRange({ from: Math.max(0, n - (narrowNow() ? 60 : 150)), to: n + 4 });
  };
  const last = series.bars[series.bars.length - 1];
  const hi = Math.max(...series.bars.map((b) => b.h));
  const lo = Math.min(...series.bars.map((b) => b.l));
  const summary = last ? `กราฟแท่งเทียน ${ins.symbol} ${series.bars.length} แท่ง ราคาปิดล่าสุด ${fmtNum(last.c, ins.priceDecimals)} ${ins.quote} สูงสุดในช่วง ${fmtNum(hi, ins.priceDecimals)} ต่ำสุด ${fmtNum(lo, ins.priceDecimals)}`
    + (drawn.wall ? ` · กำแพง ${drawn.wall} ระดับ` : "") + (drawn.big ? ` · ไม้ใหญ่ ${drawn.big} จุด` : "") : "ยังไม่มีข้อมูลกราฟ";
  const updated = updatedAt ? new Date(updatedAt).toLocaleTimeString("th-TH", { timeZone: "Asia/Bangkok", hour12: false }) : null;
  return (
    <div className={`chart-wrap${full ? " full" : ""}`} role={full ? "dialog" : undefined} aria-modal={full || undefined} aria-label={full ? `กราฟ ${ins.symbol} เต็มจอ` : undefined}>
      {full && fullHeader && <div className="chart-full-head">{fullHeader}</div>}
      <div className="chart-bar" role="toolbar" aria-label="เลื่อนและซูมกราฟ">
        <button type="button" className="cbtn" aria-label="เลื่อนไปแท่งก่อนหน้า" onClick={() => pan(-1)}>◀</button>
        <button type="button" className="cbtn" aria-label="เลื่อนไปแท่งถัดไป" onClick={() => pan(1)}>▶</button>
        <button type="button" className="cbtn" aria-label="ซูมออก" onClick={() => zoom(1.5)}>−</button>
        <button type="button" className="cbtn" aria-label="ซูมเข้า" onClick={() => zoom(1 / 1.5)}>+</button>
        <button type="button" className="cbtn" aria-label="ไปแท่งล่าสุด" onClick={toLatest}>⇥</button>
        <button type="button" className="cbtn wide-only" onClick={reset}>รีเซ็ตมุมมอง</button>
        <span className="grow" />
        <button type="button" className="cbtn" aria-label={full ? "ปิดกราฟเต็มจอ" : "ขยายกราฟเต็มจอ"} aria-pressed={full} onClick={() => setFull(!full)}>{full ? "✕" : "⤢"}</button>
      </div>
      {/* the readout sits above the chart, never over the candles; it shows the newest bar until a bar is touched */}
      <div className="chart-readout" aria-live="off">
        {legend ? <><span>{legend.ohlc}</span><span className="flow">{legend.flow}</span>{legend.wall && <span className="flow">{legend.wall}</span>}
          {oiText(details, layers, ins.market) && <span className="oi-line">{oiText(details, layers, ins.market)}</span>}
          {(updated || freshness) && <span className="meta">{updated ? `⟳ อัปเดต ${updated}` : ""}{freshness ? ` · ${freshness}` : ""}</span>}</> : <span>&nbsp;</span>}
      </div>
      <div className="chart-box" ref={box} role="img" aria-label={summary}
        data-walls={drawn.wall ?? 0} data-big={drawn.big ?? 0} data-oi={drawn.oi ?? 0} data-round={drawn.round ?? 0} data-range={drawn.range ?? 0} />
      <p className="chart-hint">{full ? "ลากเพื่อเลื่อน · บีบนิ้วเพื่อซูม · ลากแกนราคาเพื่อยืดหด · แตะค้างเพื่ออ่านค่าแท่ง" : "ลากเพื่อเลื่อน · บีบนิ้วเพื่อซูม · แตะค้างเพื่ออ่านค่าแท่ง · ⤢ เต็มจอ"}</p>
    </div>
  );
}
