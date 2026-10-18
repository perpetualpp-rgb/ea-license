// Display helpers. Times are stored in UTC and shown in Asia/Bangkok with the zone always written.

export const TZ = "Asia/Bangkok";
export const TZ_LABEL = "UTC+7";

const timeFmt = new Intl.DateTimeFormat("th-TH", { timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const hmFmt = new Intl.DateTimeFormat("th-TH", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false });
const dateFmt = new Intl.DateTimeFormat("th-TH", { timeZone: TZ, day: "numeric", month: "short", year: "numeric" });
const dtFmt = new Intl.DateTimeFormat("th-TH", { timeZone: TZ, day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });

export const fmtTime = (ms: number) => `${timeFmt.format(ms)} ${TZ_LABEL}`;
export const fmtHM = (ms: number) => hmFmt.format(ms);
export const fmtDate = (ms: number) => dateFmt.format(ms);
export const fmtDateTime = (ms: number) => `${dtFmt.format(ms)} ${TZ_LABEL}`;

/** Bangkok wall-clock offset in seconds; Thailand has no DST so this is constant */
export const BKK_OFFSET_S = 7 * 3600;

export function fmtNum(v: number | null | undefined, decimals = 2): string {
  if (v == null || !Number.isFinite(v)) return "–";
  return v.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

/** price with its quote currency written out (never a bare $) */
export const fmtPrice = (v: number | null | undefined, decimals: number, quote: string) =>
  v == null || !Number.isFinite(v) ? "ยังไม่มีข้อมูล" : `${fmtNum(v, decimals)} ${quote}`;

export function fmtSigned(v: number | null | undefined, decimals = 2, suffix = ""): string {
  if (v == null || !Number.isFinite(v)) return "–";
  const s = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${s}${fmtNum(Math.abs(v), decimals)}${suffix}`;
}

export function fmtCompact(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "–";
  const a = Math.abs(v);
  if (a >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (a >= 1e4) return `${(v / 1e3).toFixed(1)}K`;
  return fmtNum(v, a >= 100 ? 0 : a >= 1 ? 2 : 4);
}

export function fmtAge(ms: number): string {
  if (ms < 60_000) return `${Math.max(0, Math.round(ms / 1000))} วินาที`;
  if (ms < 3_600_000) return `${Math.round(ms / 60_000)} นาที`;
  if (ms < 86_400_000) return `${(ms / 3_600_000).toFixed(1)} ชั่วโมง`;
  return `${Math.round(ms / 86_400_000)} วัน`;
}
