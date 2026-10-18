import { useState } from "react";
import { AppProvider, apiToken, useApp, useRoute, go } from "./state";
import { Overview } from "./pages/Overview";
import { Analyze } from "./pages/Analyze";
import { Alerts } from "./pages/Alerts";
import { Journal } from "./pages/Journal";
import { Icon } from "./components/Icon";
import { OWNER_LABEL, Sheet, useToast } from "./components/ui";
import { TZ, TZ_LABEL } from "./lib/format";
import { ARTIFACT_BUILD } from "./lib/build";
import type { SourceMode } from "./data/source";

/** one line above every screen saying where the market data comes from */
function SourceBanner() {
  const { source, connectorIssue, retryConnector } = useApp();
  const [busy, setBusy] = useState(false);
  if (source.mode === "connector") return <div className="offline owner" role="note">{OWNER_LABEL}</div>;
  if (source.mode === "api") return <div className="offline api" role="note">ข้อมูลจากเซิร์ฟเวอร์ Flow Lab ตามสิทธิ์บัญชีสมาชิก</div>;
  return (
    <div className="offline demo" role="note">
      ต้นแบบ · ข้อมูลตลาดทั้งหมดเป็นข้อมูลตัวอย่าง ไม่ใช่ราคาจริง
      {connectorIssue && (
        <div className="source-issue" role="status">
          <span>ยังใช้ข้อมูล Binance ผ่าน connector ไม่ได้ จึงแสดงข้อมูลตัวอย่างแทน: {connectorIssue.text}</span>
          {connectorIssue.retry && (
            <button type="button" className="btn" disabled={busy} onClick={async () => { setBusy(true); try { await retryConnector(); } finally { setBusy(false); } }}>
              {busy ? "กำลังเชื่อมต่อ…" : connectorIssue.approve ? "อนุญาตและดึงข้อมูล" : "ลองเชื่อมต่ออีกครั้ง"}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

const NAV = [
  { path: "/overview", label: "ภาพรวม", icon: "home" },
  { path: "/analyze", label: "วิเคราะห์", icon: "chart" },
  { path: "/alerts", label: "แจ้งเตือน", icon: "bell" },
  { path: "/journal", label: "บันทึก", icon: "note" },
];

function Shell() {
  const { path, params } = useRoute();
  const { online, events } = useApp();
  const [settings, setSettings] = useState(false);
  const unread = events.filter((e) => !e.ack).length;
  const page = path.startsWith("/analyze") ? <Analyze params={params} /> : path.startsWith("/alerts") ? <Alerts /> : path.startsWith("/journal") ? <Journal /> : <Overview />;
  const nav = (cls: string) => NAV.map((n) => (
    <a key={n.path} href={`#${n.path}`} className={cls} aria-current={path.startsWith(n.path) ? "page" : undefined}>
      <Icon name={n.icon} />
      <span>{n.label}{n.path === "/alerts" && unread ? <span className="sr-only"> ({unread} ยังไม่อ่าน)</span> : null}</span>
      {n.path === "/alerts" && unread > 0 && <span className="badge demo" aria-hidden="true" style={{ padding: "0 6px", fontSize: 11 }}>{unread}</span>}
    </a>
  ));
  return (
    <div className="shell">
      <nav className="side-nav" aria-label="เมนูหลัก">
        <div className="brand-mark">FLOW LAB<small>Market insights · Alerts · Journal</small></div>
        {nav("nav-btn")}
        <div style={{ marginTop: "auto" }}>
          <button type="button" className="nav-btn" onClick={() => setSettings(true)}><Icon name="user" /> ตั้งค่า</button>
        </div>
      </nav>
      <div>
        {!online && <div className="offline" role="status">ออฟไลน์ — อ่านบันทึกในเครื่องได้ บันทึกใหม่จะเก็บเป็นร่างในเครื่อง</div>}
        <SourceBanner />
        <main className="main" id="main">
          {/* the account button sits in the corner of the page title row instead of taking a row of its own */}
          <button type="button" className="icon-btn top-account" aria-label="ตั้งค่าและบัญชี" onClick={() => setSettings(true)}><Icon name="user" /></button>
          {page}
        </main>
      </div>
      <nav className="bottom-nav" aria-label="เมนูหลัก">{nav("nav-btn")}</nav>
      {settings && <SettingsSheet onClose={() => setSettings(false)} />}
    </div>
  );
}

const SOURCE_HINT: Record<SourceMode, string> = {
  demo: "ข้อมูลตัวอย่างสร้างในเครื่อง ไม่ใช่ราคาจริง",
  api: "แอปเรียกข้อมูลจากเซิร์ฟเวอร์ของเราเท่านั้น ไม่เรียก exchange โดยตรงจากเครื่องสมาชิก",
  connector: "หน้านี้เรียก Binance ผ่าน connector ในบัญชี claude.ai ของคุณ อ่านเฉพาะข้อมูลตลาด (แท่งเทียน, order book, รายการซื้อขาย, OI, funding) ไม่มีคำสั่งซื้อขายหรือโอนเงิน · ดูส่วนตัว ห้ามเผยแพร่/ขาย",
};

function SettingsSheet({ onClose }: { onClose: () => void }) {
  const { settings, setSettings } = useApp();
  const [mode, setMode] = useState(settings.dataMode);
  const [url, setUrl] = useState(settings.apiUrl);
  const [token, setToken] = useState(() => apiToken.get() ?? "");
  return (
    <Sheet title="ตั้งค่า" onClose={onClose}>
      <form className="form" onSubmit={(e) => { e.preventDefault(); apiToken.set(mode === "api" ? token.trim() : ""); setSettings({ dataMode: mode, apiUrl: url.trim() }); onClose(); go("/overview"); }}>
        <label><span>แหล่งข้อมูลตลาด</span>
          <select value={mode} onChange={(e) => setMode(e.target.value as SourceMode)}>
            {ARTIFACT_BUILD && <option value="connector">Binance ผ่าน connector ของคุณ (ดูส่วนตัว)</option>}
            <option value="demo">ข้อมูลตัวอย่าง (ต้นแบบ)</option>
            <option value="api">เซิร์ฟเวอร์ Flow Lab (ต้องมี URL และบัญชี)</option>
          </select>
          <div className="hint">{SOURCE_HINT[mode]}</div></label>
        {mode === "api" && <>
          <label><span>URL ของเซิร์ฟเวอร์</span><input type="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://api.example.com" />
            <div className="hint">ยังไม่มีระบบเข้าสู่ระบบในต้นแบบนี้ ข้อมูลที่ต้องล็อกอินจะขึ้นว่า "ต้องเข้าสู่ระบบใหม่"</div></label>
          <label><span>token สำหรับทดสอบ</span><input type="password" autoComplete="off" spellCheck={false} value={token} onChange={(e) => setToken(e.target.value)} placeholder="ไม่บังคับ" />
            <div className="hint">เก็บไว้ในแท็บนี้เท่านั้น (sessionStorage) ปิดแท็บแล้วหาย ไม่บันทึกถาวรและไม่ใส่ใน URL</div></label>
        </>}
        <div><span className="field-label">เขตเวลาที่แสดง</span><b>{TZ} ({TZ_LABEL})</b></div>
        <p className="small muted" style={{ margin: 0 }}>Flow Lab เป็นเครื่องมือวิเคราะห์ ไม่ใช่คำแนะนำการลงทุน ไม่มีปุ่มซื้อขาย ไม่ได้รับรองหรือเป็นพันธมิตรกับ exchange ใด</p>
        <button type="submit" className="btn primary block">บันทึกการตั้งค่า</button>
      </form>
    </Sheet>
  );
}

export function App() {
  const [toastNode, toast] = useToast();
  return (
    <AppProvider toast={toast}>
      <a href="#main" className="sr-only">ข้ามไปเนื้อหา</a>
      <Shell />
      {toastNode}
    </AppProvider>
  );
}
