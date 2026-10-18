import { useEffect, useMemo, useState } from "react";
import { useApp, go, persisted } from "../state";
import type { AlertMetric, EvidenceTab, Instrument } from "../data/types";
import type { MarketSource } from "../data/source";
import { barDelta } from "../domain/indicators";
import { describeComparison, evaluateRule, mergeEvents, METRICS, type AlertRule, type AlertWindow, type RuleState } from "../domain/alerts";
import { Sheet, Switch, Tabs } from "../components/ui";
import { Icon } from "../components/Icon";
import { fmtDateTime, fmtHM, fmtNum } from "../lib/format";
import { newId } from "../journal/store";

const EVIDENCE: Record<AlertMetric, EvidenceTab> = { oi_change_pct: "oi", cvd_change: "chart", price_cross: "chart", wall_size: "walls", funding_rate: "oi" };
const WINDOW_MIN: Record<AlertWindow, number> = { "1m": 1, "5m": 5, "15m": 15, "1h": 60 };

/** what each metric measures right now for one instrument, from the active data source */
async function snapshotFor(source: MarketSource, id: string) {
  const [s, d] = await Promise.all([source.series(id, "1m", "6h"), source.details(id, "1m", "6h")]);
  const measure = (metric: AlertMetric, w: AlertWindow): number | null => {
    const n = WINDOW_MIN[w];
    if (metric === "cvd_change") {
      const tail = s.bars.slice(-n);
      if (tail.length < n || tail.some((b) => b.buyV == null)) return null;
      return tail.reduce((a, b) => a + barDelta(b)!, 0);
    }
    if (metric === "oi_change_pct") {
      const pts = d.oi?.series ?? [];
      const steps = Math.round(n / 5);
      if (!steps || pts.length <= steps) return null; // OI arrives every 5 minutes: a 1-minute window has no evidence
      return (pts[pts.length - 1].v / pts[pts.length - 1 - steps].v - 1) * 100;
    }
    if (metric === "price_cross") return s.bars.at(-1)?.c ?? null;
    if (metric === "wall_size") return d.walls.length ? Math.max(...d.walls.map((x) => x.size)) : null;
    return d.oi?.funding?.ratePct ?? null;
  };
  return { measure, quality: d.quality };
}

function stateText(s: RuleState, unit: string): string {
  switch (s.state) {
    case "disabled": return "ปิดอยู่";
    case "unsupported": return `ใช้ไม่ได้: ${s.reason}`;
    case "paused": return `พักชั่วคราว — ${s.reason}`;
    case "no_data": return s.reason;
    case "armed": return `ค่าล่าสุด ${fmtNum(s.measured, 2)} ${unit} · ยังไม่ถึงเกณฑ์`;
    case "cooldown": return `ถึงเกณฑ์แล้ว (${fmtNum(s.measured, 2)} ${unit}) · พักแจ้งซ้ำถึง ${fmtHM(s.until)}`;
    case "triggered": return `ถึงเกณฑ์: ${fmtNum(s.measured, 2)} ${unit}`;
  }
}

export function Alerts() {
  const { source, instruments, rules, setRules, events, setEvents } = useApp();
  const [tab, setTab] = useState<"all" | "unread" | "rules">("all");
  const [form, setForm] = useState<AlertRule | null>(null);
  const [states, setStates] = useState<Record<string, RuleState>>({});
  const [channels, setChannels] = useState(() => persisted.read("channels", { in_app: true }));

  // demo history once, merged by dedupe key so a reload never doubles it
  useEffect(() => {
    source.alertHistory().then((h) => setEvents(mergeEvents(events, h))).catch(() => undefined);
  }, [source]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    let live = true;
    const run = async () => {
      const next: Record<string, RuleState> = {};
      let fired = events;
      for (const r of rules) {
        const ins = instruments.find((i) => i.id === r.instrumentId);
        if (!ins) continue;
        try {
          const { measure, quality } = await snapshotFor(source, ins.id);
          const last = fired.filter((e) => e.ruleId === r.id).reduce<number | null>((m, e) => (m == null || e.at > m ? e.at : m), null);
          const st = evaluateRule(r, { now: Date.now(), market: ins.market, quality, measure, lastFiredAt: last, units: { qty: ins.qtyUnit, quote: ins.quote }, symbol: ins.symbol });
          next[r.id] = st;
          if (st.state === "triggered" && channels.in_app) fired = mergeEvents(fired, [st.event]);
        } catch {
          next[r.id] = { state: "no_data", reason: "โหลดข้อมูลไม่ได้" };
        }
      }
      if (!live) return;
      setStates(next);
      if (fired !== events) setEvents(fired);
    };
    run();
    const t = setInterval(run, 15_000);
    return () => { live = false; clearInterval(t); };
  }, [rules, instruments, source, channels.in_app]); // eslint-disable-line react-hooks/exhaustive-deps

  const shown = tab === "unread" ? events.filter((e) => !e.ack) : events;
  const unit = (r: AlertRule, ins?: Instrument) => METRICS[r.metric].unit(ins?.qtyUnit ?? "", ins?.quote ?? "");
  const newRule = (): AlertRule => ({ id: newId(), instrumentId: instruments[0]?.id ?? "", metric: "oi_change_pct", comparator: ">", threshold: 2, window: "5m", cooldownMin: 15, channel: "in_app", enabled: true, createdAt: Date.now() });

  return (
    <>
      <header className="page-head">
        <div className="titles"><h1>แจ้งเตือน</h1><p>แจ้งเหตุการณ์ตามเกณฑ์ที่คุณตั้ง ไม่ใช่คำสั่งซื้อขาย</p></div>
      </header>
      <button type="button" className="btn primary block" onClick={() => setForm(newRule())} disabled={!instruments.length}><Icon name="plus" /> สร้างการแจ้งเตือน</button>
      <div style={{ height: 14 }} />
      <Tabs label="มุมมองการแจ้งเตือน" value={tab} onChange={setTab} options={[{ value: "all", label: "ทั้งหมด" }, { value: "unread", label: `ยังไม่อ่าน (${events.filter((e) => !e.ack).length})` }, { value: "rules", label: `กฎของฉัน (${rules.length})` }]} />

      {tab !== "rules" ? (
        !shown.length ? <p className="empty">{tab === "unread" ? "อ่านครบทุกรายการแล้ว" : "ยังไม่มีการแจ้งเตือน"}</p> : (
          <div className="stack">
            {shown.map((e) => {
              const ins = instruments.find((i) => i.id === e.instrumentId);
              return (
                <article key={e.dedupeKey} className={`card event ${e.ack ? "" : "unread"}`}>
                  <div className="row"><b className="grow">{e.title}</b><span className="muted small num">{fmtDateTime(e.at)}</span></div>
                  <div className="cmp">{describeComparison(e)}</div>
                  <div className="row">
                    <span className={`badge ${e.live ? "live" : "caution"}`}>{e.live ? "แจ้งตอนเกิดเหตุ" : "ย้อนหลัง · ไม่ได้แจ้งสด"}</span>
                    {source.mode === "demo" && <span className="badge demo">ข้อมูลตัวอย่าง</span>}
                    {ins && <span className="tiny muted">{ins.venue} · {ins.market === "spot" ? "Spot" : "Perpetual"}</span>}
                  </div>
                  <div className="row">
                    <button type="button" className="btn" onClick={() => go("/analyze", { id: e.instrumentId, panel: EVIDENCE[e.metric] })}>เปิดดูหลักฐาน</button>
                    {!e.ack && <button type="button" className="btn ghost" onClick={() => setEvents(events.map((x) => (x.dedupeKey === e.dedupeKey ? { ...x, ack: true } : x)))}>รับทราบ</button>}
                  </div>
                </article>
              );
            })}
          </div>
        )
      ) : !rules.length ? <p className="empty">ยังไม่มีกฎ กด "สร้างการแจ้งเตือน" เพื่อเริ่ม</p> : (
        <div className="stack">
          {rules.map((r) => {
            const ins = instruments.find((i) => i.id === r.instrumentId);
            const st = states[r.id];
            const op = r.comparator === ">" ? ">" : r.comparator === "<" ? "<" : "เปลี่ยนเกิน ±";
            return (
              <article key={r.id} className="card event">
                <div className="row">
                  <b className="grow">{ins?.symbol ?? r.instrumentId} · {METRICS[r.metric].label}</b>
                  <Switch label="เปิดใช้กฎนี้" checked={r.enabled} onChange={(v) => setRules(rules.map((x) => (x.id === r.id ? { ...x, enabled: v } : x)))} />
                </div>
                <div className="small">{op} {fmtNum(r.threshold, 2)} {unit(r, ins)} · ช่วง {r.window} · พักแจ้งซ้ำ {r.cooldownMin} นาที · {r.channel === "in_app" ? "ในแอป" : "Telegram"}</div>
                <div className={`small ${st?.state === "paused" || st?.state === "unsupported" ? "error" : "muted"}`}>{st ? stateText(st, unit(r, ins)) : "กำลังตรวจ…"}</div>
                <div className="row">
                  <button type="button" className="btn" onClick={() => setForm(r)}>แก้ไข</button>
                  <button type="button" className="btn ghost danger" onClick={() => setRules(rules.filter((x) => x.id !== r.id))}>ลบ</button>
                </div>
              </article>
            );
          })}
        </div>
      )}

      <section className="section card" aria-labelledby="ch">
        <h2 id="ch" style={{ margin: "0 0 4px", fontSize: 18 }}>ช่องทางการแจ้งเตือน</h2>
        <div className="switch-row">
          <span className="ico" aria-hidden="true"><Icon name="phone" /></span>
          <span className="grow"><b>ในแอป</b><span className="tiny muted" style={{ display: "block" }}>แสดงในหน้านี้ขณะเปิดแอป</span></span>
          <Switch label="แจ้งเตือนในแอป" checked={channels.in_app} onChange={(v) => { const c = { ...channels, in_app: v }; setChannels(c); persisted.write("channels", c); }} />
        </div>
        <div className="switch-row">
          <span className="ico" aria-hidden="true"><Icon name="send" /></span>
          <span className="grow"><b>Telegram</b><span className="tiny muted" style={{ display: "block" }}>ต้องผูกบัญชีผ่านเซิร์ฟเวอร์ ยังไม่พร้อมในต้นแบบนี้</span></span>
          <Switch label="แจ้งเตือนทาง Telegram" checked={false} onChange={() => undefined} disabled />
        </div>
        <p className="caption">ถ้าข้อมูลที่กฎใช้ขาดช่วงหรือ book กำลัง resync ระบบจะพักกฎนั้นไว้ ไม่แจ้งจากข้อมูลที่เสีย</p>
      </section>

      {form && <RuleForm rule={form} instruments={instruments} onClose={() => setForm(null)}
        onSave={(r) => { setRules(rules.some((x) => x.id === r.id) ? rules.map((x) => (x.id === r.id ? r : x)) : [r, ...rules]); setForm(null); setTab("rules"); }} />}
    </>
  );
}

function RuleForm({ rule, instruments, onClose, onSave }: { rule: AlertRule; instruments: Instrument[]; onClose: () => void; onSave: (r: AlertRule) => void }) {
  const [r, setR] = useState(rule);
  const [threshold, setThreshold] = useState(String(rule.threshold));
  const [err, setErr] = useState("");
  const ins = instruments.find((i) => i.id === r.instrumentId);
  const metrics = useMemo(() => (Object.keys(METRICS) as AlertMetric[]).filter((m) => !(METRICS[m].perpOnly && ins?.market === "spot")), [ins]);
  const set = <K extends keyof AlertRule>(k: K, v: AlertRule[K]) => setR({ ...r, [k]: v });
  const unit = METRICS[r.metric].unit(ins?.qtyUnit ?? "", ins?.quote ?? "");
  const submit = () => {
    const v = Number(threshold);
    if (!threshold.trim() || !Number.isFinite(v)) return setErr("ใส่เกณฑ์เป็นตัวเลข");
    if (r.comparator === "abs>" && v <= 0) return setErr("เกณฑ์ต้องมากกว่า 0");
    onSave({ ...r, threshold: v, metric: metrics.includes(r.metric) ? r.metric : metrics[0] });
  };
  return (
    <Sheet title="ตั้งการแจ้งเตือน" onClose={onClose}>
      <form className="form" onSubmit={(e) => { e.preventDefault(); submit(); }}>
        <label><span>สินทรัพย์และตลาด</span>
          <select value={r.instrumentId} onChange={(e) => set("instrumentId", e.target.value)}>
            {instruments.map((i) => <option key={i.id} value={i.id}>{i.symbol} · {i.venue} · {i.market === "spot" ? "Spot" : "Perpetual"}</option>)}
          </select></label>
        <label><span>ข้อมูลที่ใช้</span>
          <select value={metrics.includes(r.metric) ? r.metric : metrics[0]} onChange={(e) => set("metric", e.target.value as AlertMetric)}>
            {metrics.map((m) => <option key={m} value={m}>{METRICS[m].label}</option>)}
          </select>
          <div className="hint">{METRICS[r.metric].help}</div></label>
        <div className="form-grid">
          <label><span>เงื่อนไข</span>
            <select value={r.comparator} onChange={(e) => set("comparator", e.target.value as AlertRule["comparator"])}>
              <option value=">">มากกว่า</option><option value="<">น้อยกว่า</option><option value="abs>">เปลี่ยนเกิน ± (ทั้งสองทาง)</option>
            </select></label>
          <label><span>เกณฑ์ ({unit})</span><input type="number" inputMode="decimal" step="any" value={threshold} onChange={(e) => { setThreshold(e.target.value); setErr(""); }} aria-invalid={!!err} /></label>
          <label><span>ช่วงเวลา</span>
            <select value={r.window} onChange={(e) => set("window", e.target.value as AlertWindow)}>{(["1m", "5m", "15m", "1h"] as const).map((w) => <option key={w} value={w}>{w}</option>)}</select></label>
          <label><span>พักแจ้งซ้ำ</span>
            <select value={r.cooldownMin} onChange={(e) => set("cooldownMin", Number(e.target.value))}>{[5, 15, 60].map((m) => <option key={m} value={m}>{m} นาที</option>)}</select></label>
        </div>
        {err && <div className="err" role="alert">{err}</div>}
        <label><span>ช่องทาง</span>
          <select value={r.channel} onChange={(e) => set("channel", e.target.value as AlertRule["channel"])}>
            <option value="in_app">ในแอป</option><option value="telegram" disabled>Telegram (ยังไม่พร้อม)</option>
          </select></label>
        <div className="switch-row"><span className="grow">เปิดใช้ทันที</span><Switch label="เปิดใช้" checked={r.enabled} onChange={(v) => set("enabled", v)} /></div>
        <button type="submit" className="btn primary block">บันทึกกฎ</button>
      </form>
    </Sheet>
  );
}
