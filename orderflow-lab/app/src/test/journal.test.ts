import { describe, expect, it } from "vitest";
import { JournalStore, MemoryKV, sha256, sha256Js, type MarketSnapshot } from "../journal/store";
import { entriesCsv, journalJson, tradesCsv } from "../journal/export";
import { DemoSource, DEMO_INSTRUMENTS } from "../data/demo";

const snap = (): MarketSnapshot => ({ id: "s1", instrumentId: "binance-um-btcusdt", symbol: "BTCUSDT", venue: "Binance", market: "perpetual", capturedAt: 1000,
  sourceWatermark: 900, dataKind: "demo", timeframe: "5m", lookback: "24h", layers: ["VWAP"], values: [{ label: "ราคา", value: "67,000.00 USDT" }],
  quality: [{ stream: "book", status: "ok" }], engineVersion: "demo-0.1", schemaVersion: 1, image: null });

describe("journal store", () => {
  it("a retried create with the same id does not duplicate", async () => {
    const s = new JournalStore(new MemoryKV(), () => 5000);
    const draft = { id: "e1", type: "observation" as const, marketObservedAt: 900, title: "ทดสอบ", facts: "CVD +120 BTC", hypothesis: "", tags: ["CVD"], reviewAt: null, snapshot: snap() };
    await s.create(draft);
    await s.create({ ...draft, title: "ส่งซ้ำ" });
    const list = await s.entries();
    expect(list).toHaveLength(1);
    expect(list[0].title).toBe("ทดสอบ");
    expect(list[0].syncState).toBe("local_draft");
  });

  it("edits append revisions and the snapshot cannot change", async () => {
    let now = 1000;
    const s = new JournalStore(new MemoryKV(), () => now);
    await s.create({ id: "e1", type: "observation", marketObservedAt: 900, title: "a", facts: "f", hypothesis: "h", tags: [], reviewAt: null, snapshot: snap() });
    now = 2000;
    await s.revise("e1", "review", "สิ่งที่เรียนรู้");
    await s.revise("e1", "facts", "f2");
    const [e] = await s.entries();
    expect(e.revisions.map((r) => [r.version, r.field, r.before, r.after])).toEqual([[1, "review", null, "สิ่งที่เรียนรู้"], [2, "facts", "f", "f2"]]);
    expect(e.facts).toBe("f2");
    expect(() => { (e.snapshot as MarketSnapshot).capturedAt = 1; }).toThrow();
    expect(e.snapshot!.capturedAt).toBe(1000);
  });

  it("exports CSV and JSON with the local-only note", async () => {
    const s = new JournalStore(new MemoryKV(), () => 1000);
    await s.create({ id: "e1", type: "observation", marketObservedAt: null, title: 'มี "คำพูด", และจุลภาค', facts: "", hypothesis: "", tags: ["a", "b"], reviewAt: null, snapshot: null });
    const csv = entriesCsv(await s.entries());
    expect(csv.split("\n")[1]).toContain('"มี ""คำพูด"", และจุลภาค"');
    expect(JSON.parse(journalJson(await s.entries(), [], 0)).note).toContain("ยังไม่ได้บันทึกบนเซิร์ฟเวอร์");
    expect(tradesCsv([]).trim().split("\n")).toHaveLength(1);
  });

  it("hashes images, with the same result when crypto.subtle is unavailable", async () => {
    expect(await sha256("abc")).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    const long = "ภาพ".repeat(500);
    expect(sha256Js(new TextEncoder().encode(long))).toBe(await sha256(long));
    expect(sha256Js(new Uint8Array(0))).toBe("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  });
});

describe("demo source", () => {
  const now = Date.UTC(2026, 9, 18, 3, 42, 3);
  const src = new DemoSource(() => now);
  it("is labelled demo, deterministic, and never invents data a market does not have", async () => {
    const q1 = await src.quote("binance-um-btcusdt");
    const q2 = await new DemoSource(() => now).quote("binance-um-btcusdt");
    expect(q1.freshness.kind).toBe("demo");
    expect(q1.price).toBe(q2.price);
    const spot = await src.details("binance-spot-btcusdt", "5m", "24h");
    expect(spot.oi).toBeNull();
    expect(spot.prices.find((p) => p.type === "mark")!.value).toBeNull();
    expect(spot.quality.find((q) => q.stream === "oi")!.status).toBe("unavailable");
  });
  it("a book that is resyncing shows no walls", async () => {
    const xau = await src.details("binance-um-xauusdt", "5m", "24h");
    expect(xau.quality.find((q) => q.stream === "book")!.status).toBe("book_resync");
    expect(xau.walls).toEqual([]);
  });
  it("timeframe changes do not move the CVD anchor", async () => {
    const a = await src.series("binance-um-btcusdt", "1m", "24h");
    const b = await src.series("binance-um-btcusdt", "1h", "24h");
    expect(a.cvdAnchor.startT).toBe(b.cvdAnchor.startT);
    expect(DEMO_INSTRUMENTS.find((i) => i.symbol === "XAUUSDT")!.caution).toContain("COMEX");
  });
});
