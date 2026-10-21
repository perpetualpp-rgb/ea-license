import { useEffect, useRef, useState, type ReactNode } from "react";
import { Icon } from "./Icon";
import type { DataKind, Freshness, StreamStatus } from "../data/types";
import type { SourceMode } from "../data/source";
import { fmtAge, fmtTime } from "../lib/format";

export const KIND_TEXT: Record<DataKind, string> = {
  demo: "ข้อมูลตัวอย่าง",
  live: "ข้อมูลสด",
  delayed: "ข้อมูลหน่วง",
  stale: "ข้อมูลค้าง",
  approximate: "ค่าประมาณ",
  unavailable: "ยังไม่มีข้อมูล",
};

/** the owner-preview wording, shown above every screen whenever the connector is the source */
export const OWNER_LABEL = "ข้อมูลจริงจาก Binance ผ่าน connector ของคุณ · ดูส่วนตัว ห้ามเผยแพร่/ขาย";

/** next to a price or chart: where this number came from, when it is not demo data */
export function SourceTag({ mode }: { mode: SourceMode }) {
  if (mode === "connector") return <span className="badge owner"><Icon name="shield" />Binance จริง · connector ของคุณ · ดูส่วนตัว</span>;
  if (mode === "api") return <span className="badge">เซิร์ฟเวอร์ Flow Lab</span>;
  return null;
}

/** per-asset freshness: kind of data + exchange time of the newest value, timezone always written */
export function DataBadge({ f, now }: { f: Freshness; now?: number }) {
  const cls = f.kind === "demo" ? "demo" : f.kind === "live" ? "live" : f.kind === "unavailable" ? "bad" : "caution";
  const text = f.kind === "delayed" && f.delayMin ? `ข้อมูลหน่วง ${f.delayMin} นาที` : KIND_TEXT[f.kind];
  return (
    <span className={`badge ${cls}`}>
      <Icon name={f.kind === "demo" ? "info" : "clock"} />
      {text}
      {f.asOf != null && <> · ล่าสุด <span className="num">{fmtTime(f.asOf)}</span>{now != null && f.kind !== "demo" ? ` (${fmtAge(now - f.asOf)} ก่อน)` : ""}</>}
    </span>
  );
}

export const STREAM_TEXT: Record<StreamStatus, string> = {
  ok: "ปกติ",
  stale: "ข้อมูลค้าง",
  book_resync: "book กำลัง resync",
  disconnected: "หลุดการเชื่อมต่อ",
  incomplete: "ข้อมูลไม่ครบ",
  unavailable: "ไม่มีข้อมูลนี้",
};

export function StreamBadge({ s }: { s: StreamStatus }) {
  return <span className={`badge ${s === "ok" ? "live" : s === "unavailable" ? "" : "caution"}`}>{s === "ok" ? "● " : "▲ "}{STREAM_TEXT[s]}</span>;
}

export function Side({ side }: { side: "buy" | "sell" | "bid" | "ask" }) {
  const buy = side === "buy" || side === "bid";
  const label = side === "bid" ? "BID (รอซื้อ)" : side === "ask" ? "ASK (รอขาย)" : buy ? "ซื้อ" : "ขาย";
  return <span className={`side-label ${buy ? "buy" : "sell"}`}>{buy ? "▲" : "▼"} {label}</span>;
}

interface SegProps<T extends string> { label: string; value: T; options: { value: T; label: string }[]; onChange: (v: T) => void }
export function Seg<T extends string>({ label, value, options, onChange }: SegProps<T>) {
  return (
    <div role="radiogroup" aria-label={label} className="seg">
      {options.map((o) => (
        <button key={o.value} type="button" role="radio" aria-checked={o.value === value} onClick={() => onChange(o.value)}>{o.label}</button>
      ))}
    </div>
  );
}

export function Tabs<T extends string>({ value, options, onChange, label }: { label: string; value: T; options: { value: T; label: string }[]; onChange: (v: T) => void }) {
  return (
    <div role="tablist" aria-label={label} className="tabs">
      {options.map((o) => (
        <button key={o.value} type="button" role="tab" aria-selected={o.value === value} onClick={() => onChange(o.value)}>{o.label}</button>
      ))}
    </div>
  );
}

export function Switch({ checked, onChange, label, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean }) {
  return <button type="button" role="switch" className="switch" aria-checked={checked} aria-label={label} disabled={disabled} onClick={() => onChange(!checked)} />;
}

/** bottom sheet on phones, centred dialog on wide screens; Escape closes and focus returns */
export function Sheet({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    // runs once per open: re-running on every render would steal focus while typing
    const prev = document.activeElement as HTMLElement | null;
    const first = ref.current?.querySelector<HTMLElement>("input, textarea, select, button:not(.close-btn)");
    (first ?? ref.current)?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close.current();
      if (e.key === "Tab" && ref.current) {
        const els = [...ref.current.querySelectorAll<HTMLElement>("button, input, select, textarea, a[href], [tabindex]:not([tabindex='-1'])")].filter((x) => !x.hasAttribute("disabled"));
        if (!els.length) return;
        const [a, z] = [els[0], els[els.length - 1]];
        if (e.shiftKey && document.activeElement === a) { e.preventDefault(); z.focus(); }
        else if (!e.shiftKey && document.activeElement === z) { e.preventDefault(); a.focus(); }
      }
    };
    document.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => { document.removeEventListener("keydown", onKey); document.body.style.overflow = ""; prev?.focus(); };
  }, []);
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="sheet" role="dialog" aria-modal="true" aria-label={title} ref={ref} tabIndex={-1}>
        <div className="grab" aria-hidden="true" />
        <div className="sheet-head">
          <h2>{title}</h2>
          <button type="button" className="icon-btn close-btn" aria-label="ปิด" onClick={onClose}><Icon name="x" /></button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function useToast() {
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    if (!msg) return;
    const t = setTimeout(() => setMsg(null), 4500);
    return () => clearTimeout(t);
  }, [msg]);
  const node = <div aria-live="polite" role="status">{msg && <div className="toast">{msg}</div>}</div>;
  return [node, setMsg] as const;
}

export function Spark({ values, label, up }: { values: number[]; label: string; up: boolean }) {
  if (values.length < 2) return <div className="spark muted small">ยังไม่มีข้อมูล</div>;
  const w = 300, h = 56, lo = Math.min(...values), hi = Math.max(...values), span = hi - lo || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * w},${h - 4 - ((v - lo) / span) * (h - 8)}`).join(" ");
  return (
    <svg className="spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img" aria-label={label}>
      <polyline points={pts} fill="none" stroke={up ? "var(--buy)" : "var(--sell)"} strokeWidth={2} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export function Coin({ base }: { base: string }) {
  return <span className={`coin ${base.toLowerCase() === "xau" ? "xau" : "btc"}`} aria-hidden="true">{base === "XAU" ? "Au" : "₿"}</span>;
}

export function Loading({ lines = 3 }: { lines?: number }) {
  return <div className="stack" aria-busy="true" aria-label="กำลังโหลด">{Array.from({ length: lines }, (_, i) => <div key={i} className="skeleton" style={{ height: i ? 20 : 44 }} />)}</div>;
}
