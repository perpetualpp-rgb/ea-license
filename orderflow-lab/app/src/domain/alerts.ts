// Alert rules: a measured value compared with a user threshold over a window.
// A rule whose required stream is not healthy is paused, never fired from bad data.
import type { AlertEvent, AlertMetric, StreamName, StreamQuality } from "../data/types";

export type AlertWindow = "1m" | "5m" | "15m" | "1h";
export const WINDOW_MS: Record<AlertWindow, number> = { "1m": 60e3, "5m": 300e3, "15m": 900e3, "1h": 3600e3 };

export interface AlertRule {
  id: string;
  instrumentId: string;
  metric: AlertMetric;
  comparator: ">" | "<" | "abs>";
  threshold: number;
  window: AlertWindow;
  cooldownMin: number;
  channel: "in_app" | "telegram";
  enabled: boolean;
  createdAt: number;
}

export const METRICS: Record<AlertMetric, { label: string; unit: (qty: string, quote: string) => string; needs: StreamName[]; perpOnly?: boolean; help: string }> = {
  oi_change_pct: { label: "OI เปลี่ยนแปลง", unit: () => "%", needs: ["oi"], perpOnly: true, help: "Open Interest เปลี่ยนกี่ % ในช่วงเวลาที่เลือก (หน่วยสัญญา)" },
  cvd_change: { label: "CVD เปลี่ยนแปลง", unit: (q) => q, needs: ["trades"], help: "ผลต่างปริมาณผู้กดซื้อ − ผู้กดขาย ในช่วงเวลาที่เลือก" },
  price_cross: { label: "ราคาซื้อขายล่าสุด", unit: (_q, quote) => quote, needs: ["trades"], help: "ราคา last trade ข้ามระดับที่ตั้ง" },
  wall_size: { label: "กำแพงใน order book", unit: (q) => q, needs: ["book"], help: "มีกำแพงขนาดอย่างน้อยเท่านี้ภายใน ±1% ของราคา" },
  funding_rate: { label: "Funding (ประมาณการ)", unit: () => "%", needs: ["mark"], perpOnly: true, help: "อัตรา funding ประมาณการของรอบถัดไป" },
};

export type RuleState =
  | { state: "disabled" }
  | { state: "unsupported"; reason: string }
  | { state: "paused"; reason: string }
  | { state: "no_data"; reason: string }
  | { state: "armed"; measured: number }
  | { state: "cooldown"; measured: number; until: number }
  | { state: "triggered"; measured: number; event: AlertEvent };

export interface EvalContext {
  now: number;
  market: "perpetual" | "spot";
  quality: StreamQuality[];
  /** measured value for the metric over the window, or null when there is no evidence */
  measure: (metric: AlertMetric, window: AlertWindow) => number | null;
  lastFiredAt: number | null;
  units: { qty: string; quote: string };
  symbol: string;
}

/** same rule + same window bucket → same key, so reconnects and retries cannot double-fire */
export const dedupeKey = (rule: AlertRule, now: number) => `${rule.id}:${Math.floor(now / WINDOW_MS[rule.window]) * WINDOW_MS[rule.window]}`;

export function evaluateRule(rule: AlertRule, ctx: EvalContext): RuleState {
  if (!rule.enabled) return { state: "disabled" };
  const meta = METRICS[rule.metric];
  if (meta.perpOnly && ctx.market !== "perpetual") return { state: "unsupported", reason: "ตลาด spot ไม่มีข้อมูลนี้" };
  for (const s of meta.needs) {
    const q = ctx.quality.find((x) => x.stream === s);
    if (!q || q.status !== "ok") return { state: "paused", reason: `${s}: ${q ? q.status : "ไม่มีข้อมูล"}${q?.note ? ` — ${q.note}` : ""}` };
  }
  const v = ctx.measure(rule.metric, rule.window);
  if (v == null) return { state: "no_data", reason: "ยังไม่มีข้อมูลพอสำหรับช่วงเวลานี้" };
  const hit = rule.comparator === ">" ? v > rule.threshold : rule.comparator === "<" ? v < rule.threshold : Math.abs(v) > rule.threshold;
  if (!hit) return { state: "armed", measured: v };
  const until = ctx.lastFiredAt != null ? ctx.lastFiredAt + rule.cooldownMin * 60e3 : 0;
  if (ctx.now < until) return { state: "cooldown", measured: v, until };
  const unit = meta.unit(ctx.units.qty, ctx.units.quote);
  return {
    state: "triggered", measured: v,
    event: { id: dedupeKey(rule, ctx.now), ruleId: rule.id, instrumentId: rule.instrumentId, metric: rule.metric, title: `${ctx.symbol} · ${meta.label}`,
      measured: v, threshold: rule.threshold, unit: `${unit} / ${rule.window}`, window: rule.window, at: ctx.now, live: true, dedupeKey: dedupeKey(rule, ctx.now), ack: false },
  };
}

/** add events without duplicates: the dedupe key wins over the event id */
export function mergeEvents(existing: AlertEvent[], incoming: AlertEvent[]): AlertEvent[] {
  const seen = new Set(existing.map((e) => e.dedupeKey));
  const fresh = incoming.filter((e) => !seen.has(e.dedupeKey) && seen.add(e.dedupeKey));
  return [...fresh, ...existing].sort((a, z) => z.at - a.at);
}

export function describeComparison(e: Pick<AlertEvent, "measured" | "threshold" | "unit">, comparator: AlertRule["comparator"] = ">"): string {
  const sign = e.measured > 0 && e.unit.startsWith("%") ? "+" : "";
  const op = comparator === "<" ? "<" : ">";
  return `${sign}${e.measured.toLocaleString("en-US", { maximumFractionDigits: 2 })} ${e.unit} ${op} เกณฑ์ ${e.threshold.toLocaleString("en-US")}`;
}
