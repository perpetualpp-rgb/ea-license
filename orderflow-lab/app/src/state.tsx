// App-wide state: which data adapter is in use, the local journal, alert rules and settings.
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { DemoSource } from "./data/demo";
import { ApiSource } from "./data/api";
import type { MarketSource } from "./data/source";
import type { AlertEvent, Instrument } from "./data/types";
import { IdbKV, JournalStore, MemoryKV, type KV } from "./journal/store";
import type { AlertRule } from "./domain/alerts";

export interface Settings { dataMode: "demo" | "api"; apiUrl: string }

const read = <T,>(k: string, d: T): T => {
  try { const v = localStorage.getItem("flowlab." + k); return v == null ? d : (JSON.parse(v) as T); } catch { return d; }
};
const write = (k: string, v: unknown) => {
  try { localStorage.setItem("flowlab." + k, JSON.stringify(v)); } catch { /* storage blocked: keep in memory */ }
};

function makeKV(): KV {
  try { if (typeof indexedDB !== "undefined") return new IdbKV(); } catch { /* fall through */ }
  return new MemoryKV();
}

interface Ctx {
  source: MarketSource;
  settings: Settings;
  setSettings: (s: Settings) => void;
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
  const [settings, setSettingsState] = useState<Settings>(() => read("settings", { dataMode: "demo", apiUrl: "" }));
  const source = useMemo<MarketSource>(() => (settings.dataMode === "api" ? new ApiSource(settings.apiUrl || null) : new DemoSource()), [settings]);
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

  const value: Ctx = {
    source, settings, instruments, store, journalVersion, rules, events, toast, online,
    setSettings: (s) => { setSettingsState(s); write("settings", s); },
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
