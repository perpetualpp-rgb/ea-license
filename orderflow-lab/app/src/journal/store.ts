// Personal journal kept on this device until the server exists. Every record says so:
// syncState stays "local_draft" — nothing here may claim it was saved on a server.
import type { DataKind, Lookback, MarketType, Timeframe } from "../data/types";
import type { PersonalTrade } from "../domain/accounting";

export interface MarketSnapshot {
  id: string;
  instrumentId: string;
  symbol: string;
  venue: string;
  market: MarketType;
  capturedAt: number;
  /** event time of the newest market data in the picture */
  sourceWatermark: number | null;
  dataKind: DataKind;
  timeframe: Timeframe;
  lookback: Lookback;
  layers: string[];
  values: { label: string; value: string }[];
  quality: { stream: string; status: string }[];
  engineVersion: string;
  schemaVersion: 1;
  image: { dataUrl: string; sha256: string } | null;
}

export type EntryField = "title" | "facts" | "hypothesis" | "tags" | "review";

export interface Revision {
  version: number;
  at: number;
  field: EntryField;
  before: string | null;
  after: string;
}

export interface JournalEntry {
  id: string;
  type: "observation";
  createdAt: number;
  marketObservedAt: number | null;
  title: string;
  facts: string; // what was seen, written before any outcome
  hypothesis: string;
  tags: string[];
  reviewAt: number | null;
  review: string; // written afterwards; kept apart from the reasons above
  snapshot: MarketSnapshot | null;
  revisions: Revision[];
  syncState: "local_draft";
}

export interface KV {
  get(key: string): Promise<string | null>;
  set(key: string, value: string): Promise<void>;
}

export class MemoryKV implements KV {
  private m = new Map<string, string>();
  async get(k: string) { return this.m.get(k) ?? null; }
  async set(k: string, v: string) { this.m.set(k, v); }
}

/** IndexedDB key-value store (chart images are too big for localStorage) */
export class IdbKV implements KV {
  private db: Promise<IDBDatabase>;
  constructor(name = "flowlab") {
    this.db = new Promise((res, rej) => {
      const r = indexedDB.open(name, 1);
      r.onupgradeneeded = () => r.result.createObjectStore("kv");
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error);
    });
  }
  private async tx<T>(mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
    const db = await this.db;
    return new Promise((res, rej) => {
      const req = fn(db.transaction("kv", mode).objectStore("kv"));
      req.onsuccess = () => res(req.result);
      req.onerror = () => rej(req.error);
    });
  }
  async get(k: string) { return ((await this.tx("readonly", (s) => s.get(k))) as string | undefined) ?? null; }
  async set(k: string, v: string) { await this.tx("readwrite", (s) => s.put(v, k)); }
}

const ENTRIES = "journal.v1.entries";
const TRADES = "journal.v1.trades";

export const newId = () => (globalThis.crypto?.randomUUID?.() ?? `id-${Date.now()}-${Math.random().toString(16).slice(2)}`);

export async function sha256(text: string): Promise<string> {
  const bytes = new TextEncoder().encode(text);
  if (globalThis.crypto?.subtle) {
    const buf = await crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  return sha256Js(bytes); // crypto.subtle only exists on https/localhost pages
}

const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
  0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]);

export function sha256Js(msg: Uint8Array): string {
  const len = msg.length, bits = len * 8, total = Math.ceil((len + 9) / 64) * 64;
  const buf = new Uint8Array(total);
  buf.set(msg);
  buf[len] = 0x80;
  const dv = new DataView(buf.buffer);
  dv.setUint32(total - 8, Math.floor(bits / 2 ** 32));
  dv.setUint32(total - 4, bits >>> 0);
  const h = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
  const w = new Uint32Array(64);
  const rotr = (x: number, n: number) => (x >>> n) | (x << (32 - n));
  for (let o = 0; o < total; o += 64) {
    for (let i = 0; i < 16; i++) w[i] = dv.getUint32(o + i * 4);
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3), s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, hh] = h;
    for (let i = 0; i < 64; i++) {
      const t1 = (hh + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i]) >>> 0;
      const t2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
      [hh, g, f, e, d, c, b, a] = [g, f, e, (d + t1) >>> 0, c, b, a, (t1 + t2) >>> 0];
    }
    [a, b, c, d, e, f, g, hh].forEach((v, i) => (h[i] = (h[i] + v) >>> 0));
  }
  return [...h].map((x) => x.toString(16).padStart(8, "0")).join("");
}

function deepFreeze<T>(o: T): T {
  if (o && typeof o === "object" && !Object.isFrozen(o)) {
    Object.freeze(o);
    for (const v of Object.values(o as object)) deepFreeze(v);
  }
  return o;
}

export class JournalStore {
  constructor(private kv: KV, private clock: () => number = () => Date.now()) {}

  async entries(): Promise<JournalEntry[]> {
    const raw = await this.kv.get(ENTRIES);
    const list: JournalEntry[] = raw ? JSON.parse(raw) : [];
    list.forEach((e) => e.snapshot && deepFreeze(e.snapshot));
    return list.sort((a, z) => z.createdAt - a.createdAt);
  }

  private async saveEntries(list: JournalEntry[]) {
    await this.kv.set(ENTRIES, JSON.stringify(list));
  }

  /** idempotent: creating the same id twice (a retry) returns the first record unchanged */
  async create(draft: Omit<JournalEntry, "createdAt" | "revisions" | "syncState" | "review"> & { review?: string }): Promise<JournalEntry> {
    const list = await this.entries();
    const found = list.find((e) => e.id === draft.id);
    if (found) return found;
    const entry: JournalEntry = { ...draft, review: draft.review ?? "", createdAt: this.clock(), revisions: [], syncState: "local_draft" };
    await this.saveEntries([entry, ...list]);
    if (entry.snapshot) deepFreeze(entry.snapshot);
    return entry;
  }

  /** edits never overwrite silently: each one appends a revision with the old value */
  async revise(id: string, field: EntryField, value: string): Promise<JournalEntry> {
    const list = await this.entries();
    const e = list.find((x) => x.id === id);
    if (!e) throw new Error("not found");
    const before = field === "tags" ? e.tags.join(", ") : (e[field] as string);
    if (before === value) return e;
    e.revisions.push({ version: e.revisions.length + 1, at: this.clock(), field, before: before || null, after: value });
    if (field === "tags") e.tags = value.split(",").map((t) => t.trim()).filter(Boolean);
    else e[field] = value;
    await this.saveEntries(list);
    return e;
  }

  async remove(id: string) {
    await this.saveEntries((await this.entries()).filter((e) => e.id !== id));
  }

  async trades(): Promise<PersonalTrade[]> {
    const raw = await this.kv.get(TRADES);
    return raw ? JSON.parse(raw) : [];
  }

  async saveTrade(t: PersonalTrade) {
    const list = await this.trades();
    const i = list.findIndex((x) => x.id === t.id);
    if (i >= 0) list[i] = t;
    else list.unshift(t);
    await this.kv.set(TRADES, JSON.stringify(list));
  }

  async removeTrade(id: string) {
    await this.kv.set(TRADES, JSON.stringify((await this.trades()).filter((t) => t.id !== id)));
  }
}
