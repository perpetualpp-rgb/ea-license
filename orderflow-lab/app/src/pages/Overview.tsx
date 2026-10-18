import { useEffect, useState, type ReactNode } from "react";
import { useApp, go, persisted } from "../state";
import type { Instrument, Observation, Quote } from "../data/types";
import { SourceError, errorText } from "../data/source";
import { Coin, DataBadge, Loading, SourceTag, Spark } from "../components/ui";
import { Icon } from "../components/Icon";
import { fmtNum, fmtPrice, fmtSigned, TZ_LABEL } from "../lib/format";

const MARKET_TEXT = { perpetual: "Perpetual", spot: "Spot" };
const OBS_ICON = { cvd: "bars", oi: "db", wall: "wall", bigtrade: "pulse", funding: "pulse" } as const;

export function marketLine(ins: Instrument) {
  return `${ins.venue} · ${MARKET_TEXT[ins.market]} · หน่วย ${ins.qtyUnit} · ราคาเป็น ${ins.quote}`;
}

export function Overview() {
  const { source, instruments } = useApp();
  const [sel, setSel] = useState<string>(() => persisted.read("asset", ""));
  const [quotes, setQuotes] = useState<Record<string, Quote | SourceError>>({});
  const [obs, setObs] = useState<Observation[] | SourceError | null>(null);
  const current = instruments.find((i) => i.id === sel) ?? instruments[0];

  useEffect(() => {
    let live = true;
    const load = () => instruments.forEach((i) => source.quote(i.id).then((q) => live && setQuotes((m) => ({ ...m, [i.id]: q })))
      .catch((e) => live && setQuotes((m) => ({ ...m, [i.id]: e instanceof SourceError ? e : new SourceError("server", String(e)) }))));
    load();
    const t = setInterval(load, 15_000);
    return () => { live = false; clearInterval(t); };
  }, [source, instruments]);

  useEffect(() => {
    if (!current) return;
    let live = true;
    setObs(null);
    source.observations(current.id).then((o) => live && setObs(o)).catch((e) => live && setObs(e instanceof SourceError ? e : new SourceError("server", String(e))));
    return () => { live = false; };
  }, [source, current]);

  const pick = (id: string) => { setSel(id); persisted.write("asset", id); };

  if (!instruments.length) return <><Head /><Loading lines={4} /></>;
  const q = current && quotes[current.id];
  return (
    <>
      <Head badge={q && !(q instanceof SourceError) ? <DataBadge f={{ kind: q.freshness.kind, asOf: null }} /> : null} />
      <div className="chips" role="tablist" aria-label="เลือกสินทรัพย์">
        {instruments.slice(0, 5).map((i) => {
          const qi = quotes[i.id];
          return (
            <button key={i.id} type="button" role="tab" className="chip" aria-selected={i.id === current.id} onClick={() => pick(i.id)}>
              <Coin base={i.base} />
              <span className="grow">
                <span className="sym">{i.symbol}</span> <span className="tiny muted">{MARKET_TEXT[i.market]}</span>
                <span className="small num" style={{ display: "block" }}>{qi && !(qi instanceof SourceError) ? fmtNum(qi.price, i.priceDecimals) : "–"}</span>
              </span>
            </button>
          );
        })}
      </div>

      <section className="card hero" style={{ marginTop: 12 }} aria-label={`${current.symbol} ราคา`}>
        <div className="row">
          <Coin base={current.base} />
          <div className="grow">
            <div style={{ fontWeight: 700, fontSize: 18 }}>{current.symbol}</div>
            <div className="meta">{marketLine(current)}</div>
          </div>
        </div>
        {current.caution && <p className="badge caution" style={{ marginTop: 10 }}>{current.caution}</p>}
        {!q ? <div style={{ marginTop: 12 }}><Loading lines={2} /></div> : q instanceof SourceError ? <p className="error" style={{ marginTop: 12 }}>{errorText(q)}</p> : (
          <>
            <div className="row" style={{ marginTop: 10, alignItems: "baseline" }}>
              <div className="price num grow">{fmtPrice(q.price, current.priceDecimals, current.quote)}</div>
              <div className={`change num ${q.changePct == null ? "muted" : q.changePct >= 0 ? "up" : "down"}`} style={{ fontWeight: 700 }}>
                {q.changePct == null ? "ยังไม่มีข้อมูล" : `${q.changePct >= 0 ? "▲" : "▼"} ${fmtSigned(q.changePct, 2, "%")}`} <span className="muted small">(24 ชม.)</span>
              </div>
            </div>
            <div className="meta">ราคาซื้อขายล่าสุด (last trade)</div>
            <Spark values={q.spark} up={(q.changePct ?? 0) >= 0} label={`กราฟราคา 24 ชั่วโมงของ ${current.symbol}`} />
            <div className="row" style={{ marginTop: 8 }}><DataBadge f={q.freshness} /><SourceTag mode={source.mode} /></div>
          </>
        )}
      </section>

      <section className="section" aria-labelledby="happen">
        <h2 id="happen">สิ่งที่เกิดขึ้น</h2>
        {!obs ? <Loading lines={3} /> : obs instanceof SourceError ? <p className="error">{errorText(obs)}</p> : !obs.length ? <p className="empty">ยังไม่มีเหตุการณ์ที่น่าสังเกต</p> : (
          <div className="stack">
            {obs.slice(0, 3).map((o) => (
              <button key={o.id} type="button" className="obs" onClick={() => go("/analyze", { id: o.instrumentId, panel: o.evidence })}>
                <span className="ico"><Icon name={OBS_ICON[o.kind]} /></span>
                <span>
                  <b>{o.title}</b>
                  <span className="measured num">{o.measured}</span>
                  <span className="explain" style={{ display: "block" }}>{o.explain}</span>
                  <span className="tiny" style={{ color: "var(--accent)" }}>ดูหลักฐาน</span>
                </span>
                <Icon name="chev" />
              </button>
            ))}
          </div>
        )}
        <p className="caption">ข้อสังเกตจากข้อมูล ไม่ใช่คำทำนายหรือคำแนะนำให้ซื้อขาย</p>
      </section>

      <div className="sticky-cta">
        <button type="button" className="btn primary block" onClick={() => go("/analyze", { id: current.id })}>ดูกราฟและรายละเอียด <Icon name="chev" /></button>
      </div>
    </>
  );
}

function Head({ badge }: { badge?: ReactNode }) {
  return (
    <header className="page-head">
      <div className="titles">
        <h1>ภาพรวม</h1>
        <p>ดูตลาดได้ในหน้าเดียว · เวลาแสดงเป็น {TZ_LABEL}</p>
      </div>
      {badge}
    </header>
  );
}
