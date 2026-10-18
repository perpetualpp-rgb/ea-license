import { useEffect, useMemo, useState } from "react";
import { useApp, go } from "../state";
import { Sheet, Tabs, Side } from "../components/ui";
import { Icon } from "../components/Icon";
import { newId, type JournalEntry } from "../journal/store";
import { entriesCsv, journalJson, tradesCsv } from "../journal/export";
import { evaluateTrade, type Fill, type PersonalTrade, type TradeStatus } from "../domain/accounting";
import { tradeStats } from "../domain/stats";
import { dec, toFixed } from "../domain/decimal";
import { fmtDateTime, fmtNum } from "../lib/format";
import type { Instrument } from "../data/types";

const STATUS: Record<TradeStatus, string> = { draft: "ร่าง (ยังไม่มี fill)", open: "เปิดอยู่", partially_closed: "ปิดบางส่วน", closed: "ปิดแล้ว", cancelled: "ยกเลิก" };
const TAGS = ["CVD", "OI", "กำแพง", "ไม้ใหญ่", "VWAP", "POC"];

type Downloads = { save(r: { filename: string; data: string }): Promise<unknown> } | null;
type ClaudeHost = { use(name: string): Promise<unknown> };

/** inside a claude.ai artifact the viewer confirms the save; elsewhere a normal browser download */
async function download(name: string, text: string, type: string): Promise<string | null> {
  const host = (window as unknown as { claude?: ClaudeHost }).claude;
  if (host?.use) {
    const dl = (await host.use("downloads").catch(() => null)) as Downloads;
    if (dl) {
      try { await dl.save({ filename: name, data: text }); return null; }
      catch (e) { const code = (e as { code?: string }).code; return code === "declined" ? "ยกเลิกการบันทึกไฟล์" : `บันทึกไฟล์ไม่สำเร็จ (${code ?? "ไม่ทราบสาเหตุ"})`; }
    }
  }
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return null;
}

export function Journal() {
  const { store, journalVersion, bumpJournal, toast, instruments } = useApp();
  const [tab, setTab] = useState<"obs" | "trades">("obs");
  const [entries, setEntries] = useState<JournalEntry[] | null>(null);
  const [trades, setTrades] = useState<PersonalTrade[] | null>(null);
  const [open, setOpen] = useState<JournalEntry | null>(null);
  const [writing, setWriting] = useState(false);
  const [editTrade, setEditTrade] = useState<PersonalTrade | null>(null);

  useEffect(() => {
    store.entries().then(setEntries).catch(() => setEntries([]));
    store.trades().then(setTrades).catch(() => setTrades([]));
  }, [store, journalVersion]);

  const now = Date.now();
  const weekAgo = now - 7 * 86400e3;
  const due = (entries ?? []).filter((e) => e.reviewAt && e.reviewAt <= now && !e.review).length;
  const stamp = new Date().toISOString().slice(0, 10);
  const exp = async (name: string, text: string, type: string) => { const msg = await download(name, text, type); if (msg) toast(msg); };

  return (
    <>
      <header className="page-head"><div className="titles"><h1>บันทึกของฉัน</h1><p>สิ่งที่สังเกต ตัดสินใจ และเรียนรู้</p></div></header>
      <Tabs label="ประเภทบันทึก" value={tab} onChange={setTab} options={[{ value: "obs", label: "ข้อสังเกต" }, { value: "trades", label: "เทรดส่วนตัว" }]} />

      {tab === "obs" ? (
        <>
          <div className="tiles">
            <div className="tile"><div className="k">บันทึก</div><div className="v">{entries?.length ?? "–"}</div></div>
            <div className="tile"><div className="k">สัปดาห์นี้</div><div className="v">{entries ? entries.filter((e) => e.createdAt >= weekAgo).length : "–"}</div></div>
            <div className="tile"><div className="k">ถึงเวลาทบทวน</div><div className="v" style={{ color: due ? "var(--amber-ink)" : undefined }}>{entries ? due : "–"}</div></div>
          </div>
          <div className="row" style={{ marginTop: 12 }}>
            <button type="button" className="btn primary grow" onClick={() => setWriting(true)}><Icon name="plus" /> เขียนบันทึก</button>
            <button type="button" className="btn" onClick={() => go("/analyze")}><Icon name="camera" /> จากกราฟ</button>
          </div>
          <div className="stack section">
            {!entries ? <div className="skeleton" style={{ height: 80 }} /> : !entries.length ? (
              <p className="empty">ยังไม่มีบันทึก เริ่มจากหน้า "วิเคราะห์" แล้วกด "บันทึกภาพตลาด" เพื่อเก็บภาพกราฟพร้อมเหตุผล</p>
            ) : entries.map((e) => (
              <button key={e.id} type="button" className="card" style={{ textAlign: "left", cursor: "pointer", width: "100%" }} onClick={() => setOpen(e)}>
                <div className="row"><b className="grow">{e.snapshot?.symbol ?? "บันทึก"} · <span className="num">{fmtDateTime(e.createdAt)}</span></b><Icon name="chev" /></div>
                {e.snapshot?.image && <img className="entry-thumb" src={e.snapshot.image.dataUrl} alt={`ภาพกราฟ ${e.snapshot.symbol} ณ เวลาบันทึก`} style={{ marginTop: 8 }} />}
                <div style={{ fontWeight: 700, marginTop: 8 }}>{e.title}</div>
                <div className="small muted" style={{ whiteSpace: "pre-wrap" }}>{e.facts}</div>
                <div className="row" style={{ marginTop: 8 }}>
                  {e.tags.map((t) => <span key={t} className="tag">{t}</span>)}
                  {e.snapshot?.dataKind === "demo" && <span className="badge demo">ภาพจากข้อมูลตัวอย่าง</span>}
                  <span className="badge local">ร่างในเครื่อง</span>
                  {e.reviewAt && !e.review && <span className={`badge ${e.reviewAt <= now ? "caution" : ""}`}>ทบทวน {fmtDateTime(e.reviewAt)}</span>}
                </div>
              </button>
            ))}
          </div>
        </>
      ) : (
        <Trades trades={trades} onEdit={setEditTrade} onNew={() => setEditTrade(newTrade(instruments[0]))} />
      )}

      <section className="section card" aria-labelledby="exp">
        <h2 id="exp" style={{ margin: "0 0 8px", fontSize: 18 }}>ส่งออกข้อมูลของฉัน</h2>
        <div className="row">
          <button type="button" className="btn" disabled={!entries?.length} onClick={() => exp(`flowlab-observations-${stamp}.csv`, entriesCsv(entries!), "text/csv")}><Icon name="download" /> CSV ข้อสังเกต</button>
          <button type="button" className="btn" disabled={!trades?.length} onClick={() => exp(`flowlab-trades-${stamp}.csv`, tradesCsv(trades!), "text/csv")}><Icon name="download" /> CSV เทรด</button>
          <button type="button" className="btn" disabled={!entries?.length && !trades?.length} onClick={() => exp(`flowlab-journal-${stamp}.json`, journalJson(entries ?? [], trades ?? []), "application/json")}><Icon name="download" /> JSON ทั้งหมด</button>
        </div>
        <p className="badge local" style={{ marginTop: 10, borderRadius: 10, padding: "8px 12px" }}>
          ยังไม่มีเซิร์ฟเวอร์: บันทึกทั้งหมดอยู่ในเบราว์เซอร์เครื่องนี้เท่านั้น ล้างข้อมูลเบราว์เซอร์แล้วจะหาย ควรส่งออกเก็บไว้เป็นระยะ
        </p>
      </section>

      {open && <EntrySheet entry={open} onClose={() => setOpen(null)} onChanged={(e) => { setOpen(e); bumpJournal(); }}
        onDelete={async () => { await store.remove(open.id); setOpen(null); bumpJournal(); toast("ลบบันทึกในเครื่องแล้ว"); }} />}
      {writing && <WriteSheet onClose={() => setWriting(false)} />}
      {editTrade && <TradeSheet trade={editTrade} instruments={instruments} onClose={() => setEditTrade(null)}
        onSave={async (t) => { await store.saveTrade(t); setEditTrade(null); bumpJournal(); toast("บันทึกเทรดในเครื่องแล้ว · ยังไม่ได้บันทึกบนเซิร์ฟเวอร์"); }}
        onDelete={async (id) => { await store.removeTrade(id); setEditTrade(null); bumpJournal(); }} />}
    </>
  );
}

function EntrySheet({ entry, onClose, onChanged, onDelete }: { entry: JournalEntry; onClose: () => void; onChanged: (e: JournalEntry) => void; onDelete: () => void }) {
  const { store } = useApp();
  const [review, setReview] = useState(entry.review);
  const [confirm, setConfirm] = useState(false);
  const s = entry.snapshot;
  return (
    <Sheet title={entry.title} onClose={onClose}>
      <div className="stack">
        {s && (
          <div className="card" style={{ padding: 10 }}>
            {s.image ? <img className="entry-thumb" src={s.image.dataUrl} alt={`ภาพกราฟ ${s.symbol} ณ ${fmtDateTime(s.capturedAt)}`} /> : <p className="muted small">ไม่ได้แนบภาพกราฟ</p>}
            <p className="tiny muted" style={{ margin: "6px 0" }}>
              ภาพตลาด ณ {fmtDateTime(s.capturedAt)} · {s.symbol} {s.venue} {s.market === "spot" ? "Spot" : "Perpetual"} · {s.timeframe}/{s.lookback} ·
              {s.dataKind === "demo" ? " ข้อมูลตัวอย่าง" : ` ${s.dataKind}`} · engine {s.engineVersion} · ข้อมูลล่าสุดในภาพ {s.sourceWatermark ? fmtDateTime(s.sourceWatermark) : "ไม่ทราบ"}
            </p>
            <ul style={{ margin: 0, paddingLeft: 18 }} className="small">{s.values.map((v) => <li key={v.label}>{v.label}: <span className="num">{v.value}</span></li>)}</ul>
            {s.quality.some((q) => q.status !== "ok") && <p className="error" style={{ marginTop: 8 }}>ตอนบันทึก ข้อมูลบางชนิดไม่ปกติ: {s.quality.filter((q) => q.status !== "ok").map((q) => `${q.stream} (${q.status})`).join(", ")}</p>}
          </div>
        )}
        <div><b>สิ่งที่เห็น (ก่อนรู้ผล)</b><p style={{ whiteSpace: "pre-wrap", margin: "4px 0" }}>{entry.facts || "–"}</p></div>
        <div><b>สมมติฐาน</b><p style={{ whiteSpace: "pre-wrap", margin: "4px 0" }}>{entry.hypothesis || "–"}</p></div>
        <form className="form" onSubmit={async (e) => { e.preventDefault(); onChanged(await store.revise(entry.id, "review", review.trim())); }}>
          <label><span>บททบทวน (เขียนภายหลัง แยกจากเหตุผลเดิม)</span><textarea value={review} onChange={(e) => setReview(e.target.value)} placeholder="เกิดอะไรขึ้นจริง สมมติฐานถูกหรือผิด ได้เรียนรู้อะไร" /></label>
          <button type="submit" className="btn primary" disabled={review.trim() === entry.review}>บันทึกบททบทวน</button>
        </form>
        {entry.revisions.length > 0 && (
          <details className="acc"><summary>ประวัติการแก้ไข ({entry.revisions.length}) <span className="chev"><Icon name="chev" /></span></summary>
            <div className="acc-body small">{entry.revisions.map((r) => <div key={r.version}>#{r.version} · {fmtDateTime(r.at)} · {r.field}: {r.before ? `"${r.before}" → ` : ""}"{r.after}"</div>)}</div>
          </details>
        )}
        <p className="badge local" style={{ borderRadius: 10, padding: "8px 12px" }}>ร่างในเครื่องนี้ · ยังไม่ได้บันทึกบนเซิร์ฟเวอร์</p>
        <button type="button" className="btn ghost danger" onClick={() => (confirm ? onDelete() : setConfirm(true))}>{confirm ? "กดอีกครั้งเพื่อลบถาวร" : "ลบบันทึกนี้"}</button>
      </div>
    </Sheet>
  );
}

function WriteSheet({ onClose }: { onClose: () => void }) {
  const { store, bumpJournal, toast } = useApp();
  const id = useMemo(newId, []);
  const [title, setTitle] = useState("");
  const [facts, setFacts] = useState("");
  const [hyp, setHyp] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [err, setErr] = useState("");
  return (
    <Sheet title="เขียนบันทึก" onClose={onClose}>
      <form className="form" onSubmit={async (e) => {
        e.preventDefault();
        if (!title.trim() || !facts.trim()) return setErr("ใส่ชื่อและสิ่งที่เห็น");
        await store.create({ id, type: "observation", marketObservedAt: null, title: title.trim(), facts: facts.trim(), hypothesis: hyp.trim(), tags, reviewAt: null, snapshot: null });
        bumpJournal(); toast("บันทึกเป็นร่างในเครื่องนี้แล้ว · ยังไม่ได้บันทึกบนเซิร์ฟเวอร์"); onClose();
      }}>
        <label><span>ชื่อ</span><input type="text" value={title} onChange={(e) => setTitle(e.target.value)} /></label>
        <label><span>สิ่งที่เห็น</span><textarea value={facts} onChange={(e) => setFacts(e.target.value)} /></label>
        <label><span>สมมติฐาน (ไม่บังคับ)</span><textarea value={hyp} onChange={(e) => setHyp(e.target.value)} /></label>
        <div className="toggles">{TAGS.map((t) => <button key={t} type="button" className="toggle" aria-pressed={tags.includes(t)} onClick={() => setTags(tags.includes(t) ? tags.filter((x) => x !== t) : [...tags, t])}>{t}</button>)}</div>
        {err && <div className="err" role="alert">{err}</div>}
        <p className="hint">ต้องการแนบภาพกราฟ ให้บันทึกจากหน้า "วิเคราะห์"</p>
        <button type="submit" className="btn primary block">บันทึกเป็นร่างในเครื่อง</button>
      </form>
    </Sheet>
  );
}

function newTrade(ins?: Instrument): PersonalTrade {
  return { id: newId(), instrumentId: ins?.id ?? "", symbol: ins?.symbol ?? "", side: "long", contract: ins?.contract ?? "linear", multiplier: "1", settlement: ins?.quote ?? "USDT",
    initialRisk: null, fills: [], cashflows: [], cancelled: false, source: "manual", note: "", createdAt: Date.now() };
}

function Trades({ trades, onEdit, onNew }: { trades: PersonalTrade[] | null; onEdit: (t: PersonalTrade) => void; onNew: () => void }) {
  const st = useMemo(() => tradeStats(trades ?? []), [trades]);
  const byCcy = useMemo(() => {
    const m = new Map<string, bigint>();
    for (const t of trades ?? []) { const r = evaluateTrade(t); if (r.status === "closed" && r.complete) m.set(t.settlement, (m.get(t.settlement) ?? 0n) + r.net); }
    return [...m];
  }, [trades]);
  return (
    <>
      <div className="tiles">
        <div className="tile"><div className="k">ปิดแล้ว</div><div className="v">{st.closed}</div><div className="s">เปิดอยู่ {st.open}</div></div>
        <div className="tile"><div className="k">Win rate</div><div className="v">{st.winRate == null ? "N/A" : `${(st.winRate * 100).toFixed(0)}%`}</div><div className="s">ชนะ {st.wins} · แพ้ {st.losses} · เสมอ {st.breakeven}</div></div>
        <div className="tile"><div className="k">ผลสุทธิ</div><div className="v" style={{ fontSize: 17 }}>{byCcy.length ? byCcy.map(([c, v]) => `${toFixed(v, 2)} ${c}`).join(" · ") : "N/A"}</div><div className="s">หลังค่าธรรมเนียมและ funding</div></div>
      </div>
      <p className="caption">
        Win rate = เทรดที่ปิดแล้วและผลสุทธิ &gt; 0 ÷ เทรดที่ปิดแล้วซึ่งข้อมูลครบ ({st.complete} รายการ){st.incomplete ? ` · ข้อมูลไม่ครบ ${st.incomplete} รายการไม่นับ` : ""} ·
        Net R เฉลี่ย {st.avgR == null ? "N/A (ยังไม่มีเทรดที่ล็อกความเสี่ยงเริ่มต้น)" : `${st.avgR.toFixed(2)}R จาก ${st.rCount} รายการ, มัธยฐาน ${st.medianR!.toFixed(2)}R`} ·
        Profit factor {st.profitFactor == null ? "N/A" : st.profitFactor.toFixed(2)} · Drawdown ของกำไรที่ปิดแล้ว {toFixed(st.realizedDrawdown, 2)} (ไม่ใช่ mark-to-market)
      </p>
      <button type="button" className="btn primary block" style={{ marginTop: 12 }} onClick={onNew}><Icon name="plus" /> เพิ่มเทรดที่ฉันทำเอง</button>
      <div className="stack section">
        {!trades ? <div className="skeleton" style={{ height: 60 }} /> : !trades.length ? <p className="empty">ยังไม่มีเทรด กรอกเทรดที่คุณทำจริงเอง ระบบไม่สร้างเทรดจากสัญญาณให้อัตโนมัติ</p> : trades.map((t) => {
          const r = evaluateTrade(t);
          return (
            <button key={t.id} type="button" className="card" style={{ textAlign: "left", width: "100%", cursor: "pointer" }} onClick={() => onEdit(t)}>
              <div className="row"><b className="grow">{t.symbol} · <Side side={t.side === "long" ? "buy" : "sell"} /></b><span className="badge">{STATUS[r.status]}</span></div>
              <div className="small num">สุทธิ {toFixed(r.net, 2)} {t.settlement} · R {r.netR == null ? "N/A" : toFixed(r.netR, 2)} · fill {t.fills.length} รายการ</div>
              {!r.complete && <div className="error" style={{ marginTop: 6 }}>{r.issues.join(" · ")}</div>}
              <div className="row" style={{ marginTop: 6 }}><span className="badge">กรอกเอง</span><span className="badge local">ร่างในเครื่อง</span></div>
            </button>
          );
        })}
      </div>
    </>
  );
}

function TradeSheet({ trade, instruments, onClose, onSave, onDelete }: { trade: PersonalTrade; instruments: Instrument[]; onClose: () => void; onSave: (t: PersonalTrade) => void; onDelete: (id: string) => void }) {
  const [t, setT] = useState(trade);
  const [risk, setRisk] = useState(trade.initialRisk?.amount ?? "");
  const [err, setErr] = useState("");
  const set = <K extends keyof PersonalTrade>(k: K, v: PersonalTrade[K]) => setT({ ...t, [k]: v });
  const valid = (s: string) => { try { dec(s); return true; } catch { return false; } };
  const preview = useMemo(() => { try { return evaluateTrade({ ...t, initialRisk: risk && valid(risk) ? { amount: risk, currency: t.settlement } : null }); } catch { return null; } }, [t, risk]);
  const setFill = (i: number, patch: Partial<Fill>) => set("fills", t.fills.map((f, j) => (j === i ? { ...f, ...patch } : f)));
  const pickIns = (id: string) => { const ins = instruments.find((x) => x.id === id); if (ins) setT({ ...t, instrumentId: id, symbol: ins.symbol, contract: ins.contract, settlement: ins.quote }); };
  const save = () => {
    for (const f of t.fills) if (!valid(f.qty) || !valid(f.price) || !valid(f.fee || "0") || Number(f.qty) <= 0) return setErr("fill ทุกแถวต้องมีจำนวน > 0 และราคาเป็นตัวเลข");
    if (risk && (!valid(risk) || Number(risk) <= 0)) return setErr("ความเสี่ยงเริ่มต้นต้องเป็นตัวเลขมากกว่า 0");
    if (!valid(t.multiplier) || Number(t.multiplier) <= 0) return setErr("ขนาดสัญญาต้องมากกว่า 0");
    onSave({ ...t, initialRisk: risk ? { amount: risk, currency: t.settlement } : null });
  };
  return (
    <Sheet title="เทรดส่วนตัว (กรอกเอง)" onClose={onClose}>
      <form className="form" onSubmit={(e) => { e.preventDefault(); save(); }}>
        <div className="form-grid">
          <label><span>สินทรัพย์</span><select value={t.instrumentId} onChange={(e) => pickIns(e.target.value)}>{instruments.map((i) => <option key={i.id} value={i.id}>{i.symbol} · {i.market === "spot" ? "Spot" : "Perp"}</option>)}</select></label>
          <label><span>ฝั่ง</span><select value={t.side} onChange={(e) => set("side", e.target.value as PersonalTrade["side"])}><option value="long">▲ Long (ซื้อ)</option><option value="short">▼ Short (ขาย)</option></select></label>
          <label><span>ขนาดต่อ 1 หน่วย ({t.contract === "inverse" ? `${t.settlement} ต่อสัญญา` : "หน่วยสินทรัพย์ต่อสัญญา"})</span><input type="text" inputMode="decimal" value={t.multiplier} onChange={(e) => set("multiplier", e.target.value)} />
            <div className="hint">Binance USDT-M ใช้ 1 · ห้ามเดาขนาด lot ของ CFD ให้ดูสเปกโบรกเกอร์</div></label>
          <label><span>ความเสี่ยงเริ่มต้นที่ล็อกไว้ ({t.settlement})</span><input type="text" inputMode="decimal" value={risk} onChange={(e) => setRisk(e.target.value)} placeholder="ไม่บังคับ" />
            <div className="hint">ไม่ใส่ = R เป็น N/A</div></label>
        </div>
        <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="field-label">รายการ fill (ราคาที่ได้จริง)</legend>
          <div className="fills">
            {t.fills.map((f, i) => (
              <div key={f.id} className="fill-row">
                <label><span>ประเภท</span><select value={f.action} onChange={(e) => setFill(i, { action: e.target.value as Fill["action"] })}><option value="open">เปิด/เพิ่ม</option><option value="close">ปิด/ลด</option></select></label>
                <label><span>จำนวน</span><input type="text" inputMode="decimal" value={f.qty} onChange={(e) => setFill(i, { qty: e.target.value })} /></label>
                <label><span>ราคา</span><input type="text" inputMode="decimal" value={f.price} onChange={(e) => setFill(i, { price: e.target.value })} /></label>
                <label><span>ค่าธรรมเนียม ({f.feeCurrency})</span><input type="text" inputMode="decimal" value={f.fee} onChange={(e) => setFill(i, { fee: e.target.value })} /></label>
                <label><span>เวลา</span><input type="datetime-local" value={new Date(f.ts - new Date().getTimezoneOffset() * 60e3).toISOString().slice(0, 16)} onChange={(e) => setFill(i, { ts: new Date(e.target.value).getTime() || f.ts })} /></label>
                <button type="button" className="icon-btn" aria-label={`ลบ fill แถว ${i + 1}`} onClick={() => set("fills", t.fills.filter((_, j) => j !== i))}><Icon name="x" /></button>
              </div>
            ))}
            <button type="button" className="btn" onClick={() => set("fills", [...t.fills, { id: newId(), ts: Date.now(), action: t.fills.length ? "close" : "open", qty: "", price: "", fee: "0", feeCurrency: t.settlement }])}><Icon name="plus" /> เพิ่ม fill</button>
          </div>
        </fieldset>
        <label><span>Funding รวมที่รับ(+)/จ่าย(−) ({t.settlement})</span>
          <input type="text" inputMode="decimal" value={t.cashflows[0]?.amount ?? ""} placeholder="0"
            onChange={(e) => set("cashflows", e.target.value ? [{ id: t.cashflows[0]?.id ?? newId(), ts: Date.now(), kind: "funding", amount: e.target.value, currency: t.settlement }] : [])} /></label>
        <label><span>หมายเหตุ</span><textarea value={t.note} onChange={(e) => set("note", e.target.value)} /></label>
        {!t.fills.length && <label className="check"><input type="checkbox" checked={t.cancelled} onChange={(e) => set("cancelled", e.target.checked)} /> ยกเลิก (ไม่ได้เข้าเทรด)</label>}
        {preview && (
          <div className="card small num" aria-live="polite">
            สถานะ {STATUS[preview.status]} · กำไร/ขาดทุนที่ปิดแล้ว {toFixed(preview.gross, 2)} · ค่าธรรมเนียม {toFixed(preview.fees, 2)} · funding {toFixed(preview.funding, 2)} ·
            <b> สุทธิ {toFixed(preview.net, 2)} {t.settlement}</b> · R {preview.netR == null ? "N/A" : toFixed(preview.netR, 2)}
            {preview.avgEntry != null && <> · ต้นทุนเฉลี่ยที่ยังเปิด {fmtNum(Number(toFixed(preview.avgEntry, 8)), 2)}</>}
            {!preview.complete && <div className="error" style={{ marginTop: 6 }}>{preview.issues.join(" · ")}</div>}
          </div>
        )}
        {err && <div className="err" role="alert">{err}</div>}
        <button type="submit" className="btn primary block">บันทึกในเครื่อง</button>
        <button type="button" className="btn ghost danger" onClick={() => onDelete(t.id)}>ลบเทรดนี้</button>
      </form>
    </Sheet>
  );
}
