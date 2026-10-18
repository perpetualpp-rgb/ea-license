import { useEffect, useMemo, useRef, useState } from "react";
import { useApp, go, persisted } from "../state";
import type { Lookback, MarketDetails, Quote, Series, Timeframe } from "../data/types";
import { ERROR_TEXT, SourceError } from "../data/source";
import { PriceChart, type Layers } from "../components/PriceChart";
import { Coin, DataBadge, Loading, Seg, Sheet, Side, StreamBadge } from "../components/ui";
import { Icon } from "../components/Icon";
import { GLOSSARY } from "../content/glossary";
import { cvd, vwap } from "../domain/indicators";
import { fmtAge, fmtCompact, fmtDateTime, fmtHM, fmtNum, fmtPrice, fmtSigned, fmtTime } from "../lib/format";
import { marketLine } from "./Overview";
import { newId, sha256, type MarketSnapshot } from "../journal/store";

const TFS: { value: Timeframe; label: string }[] = [{ value: "1m", label: "1m" }, { value: "5m", label: "5m" }, { value: "15m", label: "15m" }, { value: "1h", label: "1h" }];
const LBS: { value: Lookback; label: string }[] = [{ value: "6h", label: "6 ชม." }, { value: "24h", label: "24 ชม." }, { value: "3d", label: "3 วัน" }];
const PRICE_TYPE = { last_trade: "ซื้อขายล่าสุด", mark: "Mark", index: "Index" };
const WALL_STATUS = { new: "ใหม่", holding: "ยืน", adding: "เติม", pulling: "ลดลง" };
const OUTCOME = { price_reached: "ราคาไปถึง", removed: "หายไปก่อนราคาถึง", unknown: "ไม่ทราบ" };
export const ENGINE_VERSION = "demo-0.1";

type Err = SourceError;
const asErr = (e: unknown) => (e instanceof SourceError ? e : new SourceError("server", String(e)));

export function Analyze({ params }: { params: URLSearchParams }) {
  const { source, instruments } = useApp();
  const id = params.get("id") ?? persisted.read("asset", "") ?? "";
  const ins = instruments.find((i) => i.id === id) ?? instruments[0];
  const [tf, setTf] = useState<Timeframe>(() => persisted.read("tf", "5m"));
  const [lb, setLb] = useState<Lookback>(() => persisted.read("lookback", "24h"));
  const [layers, setLayers] = useState<Layers>(() => persisted.read("layers", { vwap: true, poc: false, cvd: true }));
  const [series, setSeries] = useState<Series | Err | null>(null);
  const [details, setDetails] = useState<MarketDetails | Err | null>(null);
  const [quote, setQuote] = useState<Quote | Err | null>(null);
  const [term, setTerm] = useState<string | null>(null);
  const [snapOpen, setSnapOpen] = useState(false);
  const capture = useRef<() => string | null>(() => null);
  const panel = params.get("panel");

  useEffect(() => {
    if (!ins) return;
    let live = true;
    const load = () => {
      source.series(ins.id, tf, lb).then((s) => live && setSeries(s)).catch((e) => live && setSeries(asErr(e)));
      source.details(ins.id, tf, lb).then((d) => live && setDetails(d)).catch((e) => live && setDetails(asErr(e)));
      source.quote(ins.id).then((q) => live && setQuote(q)).catch((e) => live && setQuote(asErr(e)));
    };
    load();
    const t = setInterval(load, 30_000);
    return () => { live = false; clearInterval(t); };
  }, [source, ins, tf, lb]);

  useEffect(() => { if (panel) document.getElementById(`panel-${panel}`)?.scrollIntoView({ block: "start" }); }, [panel, details]);

  const setL = (k: keyof Layers) => { const n = { ...layers, [k]: !layers[k] }; setLayers(n); persisted.write("layers", n); };
  if (!ins) return <Loading lines={6} />;
  const okSeries = series && !(series instanceof SourceError) ? series : null;
  const okDetails = details && !(details instanceof SourceError) ? details : null;
  const okQuote = quote && !(quote instanceof SourceError) ? quote : null;
  const lastCvd = okSeries ? cvd(okSeries.bars, okSeries.cvdAnchor.startT).at(-1) ?? null : null;

  const side = (
    <div className="stack">
      {okDetails && (
        <div className="price-types" aria-label="ชนิดราคา">
          {okDetails.prices.map((p) => (
            <div key={p.type}><span>{PRICE_TYPE[p.type]}</span><b>{p.value == null ? "ไม่มีในตลาดนี้" : fmtNum(p.value, ins.priceDecimals)}</b></div>
          ))}
        </div>
      )}
      <Panels details={details} ins={ins} open={panel} onTerm={setTerm} />
      <button type="button" className="btn primary block desktop-only" onClick={() => setSnapOpen(true)} disabled={!okSeries}>
        <Icon name="camera" /> บันทึกภาพตลาด
      </button>
    </div>
  );

  return (
    <>
      <header className="page-head">
        <button type="button" className="icon-btn" aria-label="กลับไปภาพรวม" onClick={() => go("/overview")}><Icon name="back" /></button>
        <div className="titles"><h1>วิเคราะห์</h1><p>หลักฐานบนกราฟ ไม่ใช่คำแนะนำซื้อขาย</p></div>
        <label className="sr-only" htmlFor="asset-pick">เลือกสินทรัพย์</label>
        <select id="asset-pick" className="btn" value={ins.id} onChange={(e) => { persisted.write("asset", e.target.value); go("/analyze", { id: e.target.value }); }}>
          {instruments.map((i) => <option key={i.id} value={i.id}>{i.symbol} · {i.market === "spot" ? "Spot" : "Perp"}</option>)}
        </select>
      </header>

      <div className="an-grid">
        <div style={{ minWidth: 0 }}>
          <section className="card" aria-label={`${ins.symbol} กราฟ`}>
            <div className="row">
              <Coin base={ins.base} />
              <div className="grow">
                <div style={{ fontWeight: 700 }}>{ins.symbol}</div>
                <div className="tiny muted">{marketLine(ins)}</div>
              </div>
              {okSeries && <DataBadge f={{ kind: okSeries.freshness.kind, asOf: null }} />}
            </div>
            {ins.caution && <p className="badge caution" style={{ marginTop: 8 }}>{ins.caution}</p>}
            {okQuote && (
              <div className="row" style={{ alignItems: "baseline", marginTop: 6 }}>
                <span className="num" style={{ fontSize: 28, fontWeight: 700 }}>{fmtPrice(okQuote.price, ins.priceDecimals, ins.quote)}</span>
                <span className={`num ${(okQuote.changePct ?? 0) >= 0 ? "buy" : "sell"}`} style={{ fontWeight: 700 }}>{(okQuote.changePct ?? 0) >= 0 ? "▲" : "▼"} {fmtSigned(okQuote.changePct, 2, "%")}</span>
                <span className="muted small">24 ชม. · ราคาซื้อขายล่าสุด</span>
              </div>
            )}
            <div className="controls">
              <div className="row"><span className="field-label" style={{ margin: 0, minWidth: 74 }}>แท่งเทียน</span>
                <Seg label="ขนาดแท่งเทียน" value={tf} options={TFS} onChange={(v) => { setTf(v); persisted.write("tf", v); }} /></div>
              <div className="row"><span className="field-label" style={{ margin: 0, minWidth: 74 }}>ช่วงข้อมูล</span>
                <Seg label="ช่วงข้อมูลย้อนหลัง" value={lb} options={LBS} onChange={(v) => { setLb(v); persisted.write("lookback", v); }} /></div>
              <div className="toggles" role="group" aria-label="ชั้นข้อมูลบนกราฟ">
                {([["vwap", "VWAP"], ["poc", "POC / Value area"], ["cvd", "CVD"]] as const).map(([k, label]) => (
                  <span key={k} className="row" style={{ gap: 0 }}>
                    <button type="button" className="toggle" aria-pressed={layers[k]} onClick={() => setL(k)}>{layers[k] ? "✓ " : ""}{label}</button>
                    <button type="button" className="info-btn" aria-label={`อธิบาย ${label}`} onClick={() => setTerm(k === "poc" ? "poc" : k)}><Icon name="info" /></button>
                  </span>
                ))}
              </div>
            </div>
            {!series ? <Loading lines={6} /> : series instanceof SourceError ? <p className="error">{ERROR_TEXT[series.code]}</p> : (
              <>
                <PriceChart ins={ins} series={series} layers={layers} viewKey={`${ins.id}|${tf}|${lb}`}
                  profile={okDetails?.profile ?? null} onCapture={(fn) => (capture.current = fn)} />
                <p className="caption">
                  {layers.cvd && <>แผงล่าง: CVD · {series.cvdAnchor.label} · หน่วย {ins.qtyUnit} · ล่าสุด {lastCvd == null ? "ไม่ทราบ (ไม่มีข้อมูลฝั่งผู้กด)" : fmtSigned(lastCvd, 1, ` ${ins.qtyUnit}`)}<br /></>}
                  {layers.vwap && <>VWAP: {series.vwapMethod}<br /></>}
                  {layers.poc && okDetails?.profile && <>POC: {okDetails.profile.method} · ช่วงละ {okDetails.profile.binSize} {ins.quote}<br /></>}
                  เวลาบนแกนเป็น UTC+7 · ลากเพื่อเลื่อน ล้อเมาส์หรือบีบนิ้วเพื่อซูม
                </p>
              </>
            )}
          </section>
          <div className="sticky-cta mobile-only">
            <button type="button" className="btn primary block" onClick={() => setSnapOpen(true)} disabled={!okSeries}><Icon name="camera" /> บันทึกภาพตลาด</button>
          </div>
        </div>
        <aside className="an-side" aria-label="รายละเอียด">{side}</aside>
      </div>

      {term && GLOSSARY[term] && (
        <Sheet title={GLOSSARY[term].title} onClose={() => setTerm(null)}>
          <div className="stack">
            <p style={{ margin: 0 }}>{GLOSSARY[term].what}</p>
            <p className="card small" style={{ margin: 0 }}><b>ตัวอย่าง:</b> {GLOSSARY[term].example}</p>
            <p className="badge caution" style={{ margin: 0, borderRadius: 12, padding: "10px 12px" }}>{GLOSSARY[term].not}</p>
          </div>
        </Sheet>
      )}
      {snapOpen && okSeries && (
        <SnapshotForm onClose={() => setSnapOpen(false)} build={async (withImage) => {
          const last = okSeries.bars[okSeries.bars.length - 1];
          const vw = vwap(okSeries.bars, okSeries.cvdAnchor.startT).at(-1) ?? null;
          const img = withImage ? capture.current() : null;
          const snap: MarketSnapshot = {
            id: newId(), instrumentId: ins.id, symbol: ins.symbol, venue: ins.venue, market: ins.market, capturedAt: Date.now(),
            sourceWatermark: okSeries.freshness.asOf, dataKind: okSeries.freshness.kind, timeframe: tf, lookback: lb,
            layers: Object.entries(layers).filter(([, v]) => v).map(([k]) => k.toUpperCase()),
            values: [
              { label: "ราคาซื้อขายล่าสุด", value: fmtPrice(last.c, ins.priceDecimals, ins.quote) },
              { label: "VWAP", value: vw == null ? "ยังไม่มีข้อมูล" : fmtPrice(vw, ins.priceDecimals, ins.quote) },
              { label: `CVD (${okSeries.cvdAnchor.label})`, value: lastCvd == null ? "ไม่ทราบ" : fmtSigned(lastCvd, 1, ` ${ins.qtyUnit}`) },
              ...(okDetails?.profile?.poc != null ? [{ label: "POC", value: fmtPrice(okDetails.profile.poc, ins.priceDecimals, ins.quote) }] : []),
              ...(okDetails?.oi?.value != null ? [{ label: "OI", value: `${fmtCompact(okDetails.oi.value)} ${okDetails.oi.unit}` }] : []),
            ],
            quality: okDetails?.quality.map((q) => ({ stream: q.stream, status: q.status })) ?? [],
            engineVersion: ENGINE_VERSION, schemaVersion: 1,
            image: img ? { dataUrl: img, sha256: await sha256(img) } : null,
          };
          return { snap, observedAt: last.t * 1000 };
        }} symbol={ins.symbol} />
      )}
    </>
  );
}

function Panels({ details, ins, open, onTerm }: { details: MarketDetails | Err | null; ins: { qtyUnit: string; quote: string; priceDecimals: number; market: string }; open: string | null; onTerm: (t: string) => void }) {
  if (!details) return <Loading lines={3} />;
  if (details instanceof SourceError) return <p className="error">{ERROR_TEXT[details.code]}</p>;
  const now = Date.now();
  const book = details.quality.find((q) => q.stream === "book");
  return (
    <div>
      <details className="acc" id="panel-walls" open={open === "walls"}>
        <summary><Icon name="wall" /> กำแพงและไม้ใหญ่ <span className="chev"><Icon name="chev" /></span></summary>
        <div className="acc-body">
          <div className="row"><b className="grow">กำแพงตอนนี้</b><button type="button" className="link-btn" onClick={() => onTerm("wall")}>คืออะไร</button></div>
          {book && book.status !== "ok" ? <p className="error">{ins.market === "spot" && book.status === "unavailable" ? "ไม่มีข้อมูล order book" : `book: ${book.note ?? book.status} — ไม่แสดงกำแพงจาก book ที่ไม่ซิงก์`}</p>
            : !details.walls.length ? <p className="muted small">ยังไม่พบกำแพงตามเกณฑ์</p> : (
            <table className="data"><thead><tr><th>ฝั่ง</th><th className="r">ราคา</th><th className="r">ขนาด</th><th>สถานะ</th></tr></thead>
              <tbody>{details.walls.map((w, i) => <tr key={i}><td><Side side={w.side} /></td><td className="r">{fmtNum(w.price, ins.priceDecimals)}</td><td className="r">{fmtNum(w.size, 1)} {ins.qtyUnit}</td><td>{WALL_STATUS[w.status]} · {fmtAge(w.ageSec * 1000)}</td></tr>)}</tbody></table>
          )}
          <p className="caption">{details.wallMethod}</p>
          <b>กำแพงที่หายไปล่าสุด</b>
          <table className="data"><tbody>{details.wallEvents.map((e, i) => <tr key={i}><td><Side side={e.side} /></td><td className="r">{fmtNum(e.price, ins.priceDecimals)}</td><td>{OUTCOME[e.outcome]}</td><td className="r muted">{fmtHM(e.at)}</td></tr>)}</tbody></table>
          <p className="caption">ระบบรู้แค่ว่าราคาไปถึงระดับนั้นตอนกำแพงหายหรือไม่ ไม่รู้ว่าคำสั่งถูกจับคู่หรือถูกถอน</p>
          <div className="row"><b className="grow">ไม้ใหญ่ล่าสุด</b><button type="button" className="link-btn" onClick={() => onTerm("bigtrade")}>คืออะไร</button></div>
          <table className="data"><thead><tr><th>เวลา</th><th>ฝั่ง</th><th className="r">ราคา</th><th className="r">ขนาด</th></tr></thead>
            <tbody>{details.bigTrades.map((t, i) => <tr key={i}><td className="muted">{fmtHM(t.at)}</td><td><Side side={t.side} /></td><td className="r">{fmtNum(t.price, ins.priceDecimals)}</td><td className="r">{fmtNum(t.size, 1)} {ins.qtyUnit}</td></tr>)}</tbody></table>
          <p className="caption">{details.bigMethod}</p>
        </div>
      </details>

      <details className="acc" id="panel-oi" open={open === "oi"}>
        <summary><Icon name="db" /> OI และ Funding <span className="chev"><Icon name="chev" /></span></summary>
        <div className="acc-body">
          {!details.oi ? <p className="muted">ตลาดนี้ไม่มี Open Interest และ funding (มีเฉพาะสัญญา futures)</p> : (
            <>
              <table className="data"><tbody>
                <tr><td>Open Interest</td><td className="r">{fmtCompact(details.oi.value)} {details.oi.unit}</td></tr>
                <tr><td>เปลี่ยนใน 5 นาที</td><td className="r">{fmtSigned(details.oi.change5mPct, 2, "%")}</td></tr>
                <tr><td>เปลี่ยนใน 1 ชั่วโมง</td><td className="r">{fmtSigned(details.oi.change1hPct, 2, "%")}</td></tr>
                <tr><td>Funding ({details.oi.funding?.kind === "settled" ? "ชำระแล้ว" : "ประมาณการรอบถัดไป"})</td><td className="r">{details.oi.funding ? fmtSigned(details.oi.funding.ratePct, 4, "%") : "ยังไม่มีข้อมูล"}</td></tr>
                {details.oi.funding?.nextAt && <tr><td>รอบชำระถัดไป</td><td className="r">{fmtTime(details.oi.funding.nextAt)}</td></tr>}
              </tbody></table>
              <div className="row" style={{ marginTop: 6 }}>
                <button type="button" className="link-btn" onClick={() => onTerm("oi")}>OI คืออะไร</button>
                <button type="button" className="link-btn" onClick={() => onTerm("funding")}>Funding คืออะไร</button>
              </div>
            </>
          )}
        </div>
      </details>

      <details className="acc" id="panel-quality" open={open === "quality"}>
        <summary><Icon name="shield" /> คุณภาพข้อมูล <span className="chev"><Icon name="chev" /></span></summary>
        <div className="acc-body">
          <table className="data"><tbody>{details.quality.map((q) => (
            <tr key={q.stream}><td>{q.stream}</td><td><StreamBadge s={q.status} />{q.note && <div className="tiny muted">{q.note}</div>}</td>
              <td className="r muted tiny">{q.lastAt ? `${fmtAge(now - q.lastAt)} ก่อน` : "–"}</td></tr>
          ))}</tbody></table>
          <button type="button" className="link-btn" onClick={() => onTerm("quality")}>อ่านคำอธิบาย</button>
        </div>
      </details>
    </div>
  );
}

const TAGS = ["CVD", "OI", "กำแพง", "ไม้ใหญ่", "VWAP", "POC"];
const REVIEW = [["", "ไม่ตั้ง"], ["1", "อีก 1 ชั่วโมง"], ["4", "อีก 4 ชั่วโมง"], ["24", "พรุ่งนี้"]] as const;

function SnapshotForm({ onClose, build, symbol }: { onClose: () => void; symbol: string; build: (withImage: boolean) => Promise<{ snap: MarketSnapshot; observedAt: number }> }) {
  const { store, bumpJournal, toast } = useApp();
  const entryId = useMemo(newId, []); // one id per form: a double tap or retry cannot create two entries
  const [title, setTitle] = useState(`${symbol} · ${fmtHM(Date.now())}`);
  const [facts, setFacts] = useState("");
  const [hyp, setHyp] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [review, setReview] = useState("");
  const [withImage, setWithImage] = useState(true);
  const [preview, setPreview] = useState<{ snap: MarketSnapshot; observedAt: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    build(withImage).then(setPreview).catch((e) => setErr(`เตรียมภาพตลาดไม่สำเร็จ: ${String(e)}`));
  }, [withImage]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = async () => {
    if (!facts.trim()) { setErr("ใส่สิ่งที่เห็นอย่างน้อยหนึ่งบรรทัด"); return; }
    if (!preview) return;
    setBusy(true);
    try {
      await store.create({ id: entryId, type: "observation", marketObservedAt: preview.observedAt, title: title.trim() || symbol, facts: facts.trim(), hypothesis: hyp.trim(),
        tags, reviewAt: review ? Date.now() + Number(review) * 3600e3 : null, snapshot: preview.snap });
      bumpJournal();
      toast("บันทึกเป็นร่างในเครื่องนี้แล้ว · ยังไม่ได้บันทึกบนเซิร์ฟเวอร์");
      onClose();
    } catch (e) {
      setErr(`บันทึกไม่สำเร็จ: ${String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet title="บันทึกภาพตลาด" onClose={onClose}>
      <form className="form" onSubmit={(e) => { e.preventDefault(); save(); }}>
        <label><span>ชื่อ</span><input type="text" value={title} onChange={(e) => setTitle(e.target.value)} maxLength={120} /></label>
        <label><span>สิ่งที่เห็น (ข้อเท็จจริง)</span><textarea value={facts} onChange={(e) => { setFacts(e.target.value); setErr(""); }} placeholder="เช่น CVD +120 BTC ใน 5 นาที ขณะราคายืนเหนือ VWAP" aria-invalid={!!err} />
          {err && <div className="err" role="alert">{err}</div>}</label>
        <label><span>สมมติฐานของฉัน (ไม่บังคับ)</span><textarea value={hyp} onChange={(e) => setHyp(e.target.value)} placeholder="สิ่งที่คิดว่าอาจเกิด และจะรู้ได้อย่างไรว่าผิด" /></label>
        <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="field-label">แท็ก</legend>
          <div className="toggles">{TAGS.map((t) => <button key={t} type="button" className="toggle" aria-pressed={tags.includes(t)} onClick={() => setTags(tags.includes(t) ? tags.filter((x) => x !== t) : [...tags, t])}>{t}</button>)}</div>
        </fieldset>
        <label><span>นัดทบทวน</span><select value={review} onChange={(e) => setReview(e.target.value)}>{REVIEW.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
        <label className="check"><input type="checkbox" checked={withImage} onChange={(e) => setWithImage(e.target.checked)} /> แนบภาพกราฟ ณ ตอนนี้</label>
        {preview && (
          <div className="card small" style={{ padding: 10 }}>
            {preview.snap.image && <img className="entry-thumb" src={preview.snap.image.dataUrl} alt={`ภาพกราฟ ${symbol} ที่จะบันทึก`} />}
            <div className="muted tiny" style={{ marginTop: 6 }}>ภาพ ณ {fmtDateTime(preview.snap.capturedAt)} · {preview.snap.dataKind === "demo" ? "ข้อมูลตัวอย่าง" : preview.snap.dataKind} · {preview.snap.timeframe}/{preview.snap.lookback} · engine {preview.snap.engineVersion}</div>
            <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>{preview.snap.values.map((v) => <li key={v.label}>{v.label}: <span className="num">{v.value}</span></li>)}</ul>
          </div>
        )}
        <p className="badge local" style={{ borderRadius: 10, padding: "8px 12px" }}>ยังไม่มีเซิร์ฟเวอร์: บันทึกจะเก็บเป็นร่างในเครื่องนี้เท่านั้น</p>
        <button type="submit" className="btn primary block" disabled={busy || !preview}>{busy ? "กำลังบันทึก…" : "บันทึกเป็นร่างในเครื่อง"}</button>
      </form>
    </Sheet>
  );
}
