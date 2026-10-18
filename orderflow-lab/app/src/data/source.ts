// The one interface screens use for market data. Swap DemoSource for ApiSource without touching UI.
import type { AlertEvent, Instrument, Lookback, MarketDetails, Observation, Quote, Series, Timeframe } from "./types";

export interface MarketSource {
  readonly mode: "demo" | "api";
  instruments(): Promise<Instrument[]>;
  quote(id: string): Promise<Quote>;
  series(id: string, tf: Timeframe, lookback: Lookback): Promise<Series>;
  observations(id: string): Promise<Observation[]>;
  details(id: string, tf: Timeframe, lookback: Lookback): Promise<MarketDetails>;
  alertHistory(): Promise<AlertEvent[]>;
}

/** A failure the UI can explain; `code` decides the wording, never a silent zero. */
export class SourceError extends Error {
  constructor(public code: "not_configured" | "unauthorized" | "rate_limited" | "network" | "not_supported" | "server", message: string) {
    super(message);
  }
}

export const ERROR_TEXT: Record<SourceError["code"], string> = {
  not_configured: "ยังไม่ได้เชื่อมต่อเซิร์ฟเวอร์ข้อมูล",
  unauthorized: "ต้องเข้าสู่ระบบใหม่",
  rate_limited: "ขอข้อมูลถี่เกินไป ระบบจะลองใหม่อีกครั้ง",
  network: "เชื่อมต่อเครือข่ายไม่ได้",
  not_supported: "ตลาดหรือสัญญานี้ยังไม่รองรับ",
  server: "เซิร์ฟเวอร์ตอบผิดพลาด",
};
