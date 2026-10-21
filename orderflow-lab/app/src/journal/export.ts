// Export of the user's own records. CSV is the flat table; JSON keeps every field and revision.
import { toFixed } from "../domain/decimal";
import { evaluateTrade, type PersonalTrade } from "../domain/accounting";
import type { JournalEntry } from "./store";

const csvCell = (v: unknown) => {
  const s = v == null ? "" : String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};
const iso = (ms: number | null | undefined) => (ms ? new Date(ms).toISOString() : "");

export function entriesCsv(entries: JournalEntry[]): string {
  const head = ["id", "created_at_utc", "market_observed_at_utc", "title", "facts", "hypothesis", "review", "tags", "review_at_utc",
    "snapshot_symbol", "snapshot_market", "snapshot_captured_at_utc", "snapshot_data_kind", "snapshot_engine_version", "revisions", "sync_state"];
  const rows = entries.map((e) => [e.id, iso(e.createdAt), iso(e.marketObservedAt), e.title, e.facts, e.hypothesis, e.review, e.tags.join("; "), iso(e.reviewAt),
    e.snapshot?.symbol, e.snapshot?.market, iso(e.snapshot?.capturedAt), e.snapshot?.dataKind, e.snapshot?.engineVersion, e.revisions.length, e.syncState]);
  return [head, ...rows].map((r) => r.map(csvCell).join(",")).join("\n") + "\n";
}

export function tradesCsv(trades: PersonalTrade[]): string {
  const head = ["id", "symbol", "side", "contract", "status", "settlement", "gross", "fees", "funding", "net", "net_r", "complete", "fills", "closed_at_utc", "source"];
  const rows = trades.map((t) => {
    const r = evaluateTrade(t);
    return [t.id, t.symbol, t.side, t.contract, r.status, t.settlement, toFixed(r.gross, 8), toFixed(r.fees, 8), toFixed(r.funding, 8), toFixed(r.net, 8),
      r.netR == null ? "N/A" : toFixed(r.netR, 4), r.complete, t.fills.length, iso(r.closedAt), t.source];
  });
  return [head, ...rows].map((r) => r.map(csvCell).join(",")).join("\n") + "\n";
}

export function journalJson(entries: JournalEntry[], trades: PersonalTrade[], exportedAt = Date.now()): string {
  return JSON.stringify({ format: "flowlab-journal", schemaVersion: 1, exportedAt: new Date(exportedAt).toISOString(),
    note: "ข้อมูลในเครื่องนี้เท่านั้น ยังไม่ได้บันทึกบนเซิร์ฟเวอร์", entries, trades }, null, 2);
}
