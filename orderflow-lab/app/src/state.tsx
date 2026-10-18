// App-wide state: which data adapter is in use, the local journal, alert rules and settings.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { DemoSource } from "./data/demo";
import { ApiSource } from "./data/api";
import type { ConnectorIssue, MarketSource, SourceMode } from "./data/source";
import type { AlertEvent, Instrument, Lookback, Timeframe } from "./data/types";
import { IdbKV, JournalStore, MemoryKV, type KV } from "./journal/store";
import type { AlertRule } from "./domain/alerts";
import { ARTIFACT_BUILD } from "./lib/build";

export interface Settings { dataMode: SourceMode; apiUrl: string }

const read = <T,>(k: string, d: T): T => {
  try { const v = localStorage.getItem("flowlab." + k); return v == null ? d : (JSON.parse(v) as T); } catch { return d; }
};
const write = (k: string, v: unknown) => {
  try { localStorage.setItem("flowlab." + k, JSON.stringify(v)); } catch { /* storage blocked: keep in memory */ }
};

// The API test token lives in this tab's sessionStorage only: never localStorage, never the URL.
const TOKEN_KEY = "flowlab.apiToken";
export const apiToken = {
  get(): string | null { try { return sessionStorage.getItem(TOKEN_KEY) || null; } catch { return null; } },
  set(v: string) { try { if (v) sessionStorage.setItem(TOKEN_KEY, v); else sessionStorage.removeItem(TOKEN_KEY); } catch { /* blocked: no token */ } },
};

function makeKV(): KV {
  try { if (typeof indexedDB !== "undefined") return new IdbKV(); } catch { /* fall through */ }
  return new MemoryKV();
}

// ---- owner preview through the claude.ai connector (artifact build only) --------------------
// The literal MODE check sits right here so Rollup drops the dynamic import, and with it every
// line of connector code, from the production bundle.
const loadConnector = import.meta.env.MODE === "artifact" ? () => import("./data/connector") : null;

type ClaudeHost = { use(name: string): Promise<unknown> };
const host = (): ClaudeHost | undefined => (typeof window === "undefined" ? undefined : (window as unknown as { claude?: ClaudeHost }).claude);
const hasHost = () => typeof host()?.use === "function";
const NO_HOST: ConnectorIssue = { code: "no_host", text: "เปิดหน้านี้ใน claude.ai เพื่อให้ดึงข้อมูลผ่าน connector Binance ของคุณได้", approve: false, retry: false };

type ConnectorLike = MarketSource & { probe(): Promise<ConnectorIssue | null> };

/** stands in for ConnectorSource while its module loads; one instance keeps the wall trackers for the whole visit */
class LazyConnector implements MarketSource {
  readonly mode = "connector" as const;
  private inner: Promise<ConnectorLike>;
  constructor(load: NonNullable<typeof loadConnector>, onIssue: (i: ConnectorIssue) => void) {
    this.inner = load().then((m) => new m.ConnectorSource(() => host()!.use("mcp") as Promise<never>, { onIssue }));
  }
  instruments() { return this.inner.then((s) => s.instruments()); }
  quote(id: string) { return this.inner.then((s) => s.quote(id)); }
  series(id: string, tf: Timeframe, lb: Lookback) { return this.inner.then((s) => s.series(id, tf, lb)); }
  observations(id: string) { return this.inner.then((s) => s.observations(id)); }
  details(id: string, tf: Timeframe, lb: Lookback) { return this.inner.then((s) => s.details(id, tf, lb)); }
  alertHistory() { return this.inner.then((s) => s.alertHistory()); }
  probe() { return this.inner.then((s) => s.probe()); }
}

/** artifact build: the owner's connector by default; production: demo unless an API URL is set */
const defaultSettings = (): Settings => ({ dataMode: ARTIFACT_BUILD ? "connector" : "demo", apiUrl: "" });
// the artifact keeps its own key so a choice saved by an older demo-only artifact does not hide the connector
const SETTINGS_KEY = ARTIFACT_BUILD ? "settings.artifact" : "settings";

interface Ctx {
  source: MarketSource;
  settings: Settings;
  setSettings: (s: Settings) => void;
  /** the connector was chosen but cannot be used: demo data is shown and this says why */
  connectorIssue: ConnectorIssue | null;
  retryConnector: () => Promise<void>;
  instruments: Instrument[];
  store: JournalStore;
  journalVersion: number;
  bumpJournal: () => void;
  rules: AlertRule[];
  setRules: (r: AlertRule[]) => void;
  events: AlertEvent[];
  setEvents: (e: AlertEvent[]) => void;
  toast: (msg: string) => void;
  online: boolean;
}

const AppCtx = createContext<Ctx | null>(null);
export const useApp = () => useContext(AppCtx)!;

export function AppProvider({ children, toast }: { children: ReactNode; toast: (m: string) => void }) {
  const [settings, setSettingsState] = useState<Settings>(() => {
    const s = read<Settings>(SETTINGS_KEY, defaultSettings());
    return s.dataMode === "connector" && !loadConnector ? { ...s, dataMode: "demo" } : s;
  });
  const [issue, setIssue] = useState<ConnectorIssue | null>(() => (loadConnector && !hasHost() ? NO_HOST : null));
  const conn = useRef<LazyConnector | null>(null);
  const getConn = () => {
    if (!conn.current && loadConnector) conn.current = new LazyConnector(loadConnector, (i) => setIssue((prev) => prev ?? i));
    return conn.current;
  };
  const source = useMemo<MarketSource>(() => {
    if (settings.dataMode === "api") return new ApiSource(settings.apiUrl || null, () => apiToken.get());
    if (settings.dataMode === "connector" && !issue) { const c = getConn(); if (c) return c; }
    return new DemoSource();
  }, [settings, issue]); // eslint-disable-line react-hooks/exhaustive-deps
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const store = useMemo(() => new JournalStore(makeKV()), []);
  const [journalVersion, setJV] = useState(0);
  const [rules, setRulesState] = useState<AlertRule[]>(() => read("rules", []));
  const [events, setEventsState] = useState<AlertEvent[]>(() => read("events", []));
  const [online, setOnline] = useState(typeof navigator === "undefined" ? true : navigator.onLine);

  useEffect(() => {
    let live = true;
    source.instruments().then((l) => live && setInstruments(l)).catch(() => live && setInstruments([]));
    return () => { live = false; };
  }, [source]);
  useEffect(() => {
    const on = () => setOnline(true), off = () => setOnline(false);
    window.addEventListener("online", on);
    window.addEventListener("offline", off);
    return () => { window.removeEventListener("online", on); window.removeEventListener("offline", off); };
  }, []);

  /** from a click: one call (claude.ai may ask the owner to allow Binance); back to the connector when it works */
  const retryConnector = async () => {
    if (!hasHost()) { setIssue(NO_HOST); return; }
    const c = getConn();
    if (!c) return;
    const res = await c.probe();
    setIssue(res);
  };

  const value: Ctx = {
    source, settings, instruments, store, journalVersion, rules, events, toast, online,
    connectorIssue: settings.dataMode === "connector" ? issue : null,
    retryConnector,
    setSettings: (s) => { setSettingsState(s); write(SETTINGS_KEY, s); },
    bumpJournal: useCallback(() => setJV((v) => v + 1), []),
    setRules: (r) => { setRulesState(r); write("rules", r); },
    setEvents: (e) => { setEventsState(e); write("events", e.slice(0, 200)); },
  };
  return <AppCtx.Provider value={value}>{children}</AppCtx.Provider>;
}

/** tiny hash router: #/overview, #/analyze?id=..&panel=walls, #/alerts, #/journal */
export function useRoute() {
  const parse = () => {
    const [path, q] = (location.hash.replace(/^#/, "") || "/overview").split("?");
    return { path: path || "/overview", params: new URLSearchParams(q || "") };
  };
  const [route, setRoute] = useState(parse);
  useEffect(() => {
    const on = () => { setRoute(parse()); window.scrollTo(0, 0); };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

export const go = (path: string, params?: Record<string, string>) => {
  location.hash = path + (params ? "?" + new URLSearchParams(params).toString() : "");
};

export const persisted = { read, write };
