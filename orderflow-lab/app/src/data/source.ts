// The one interface screens use for market data. Swap DemoSource for ApiSource without touching UI.
import type { AlertEvent, Instrument, Lookback, MarketDetails, Observation, Quote, Series, Timeframe } from "./types";

/**
 * "demo" = labelled fake data, "api" = the Flow Lab server (members),
 * "connector" = the owner's own Binance connector inside a private claude.ai artifact (artifact build only).
 */
export type SourceMode = "demo" | "api" | "connector";

export interface MarketSource {
  readonly mode: SourceMode;
  instruments(): Promise<Instrument[]>;
  quote(id: string): Promise<Quote>;
  series(id: string, tf: Timeframe, lookback: Lookback): Promise<Series>;
  observations(id: string): Promise<Observation[]>;
  details(id: string, tf: Timeframe, lookback: Lookback): Promise<MarketDetails>;
  alertHistory(): Promise<AlertEvent[]>;
}

/** A failure the UI can explain; `code` decides the wording, never a silent zero. */
export class SourceError extends Error {
  constructor(
    public code: "not_configured" | "unauthorized" | "rate_limited" | "network" | "not_supported" | "server",
    message: string,
    /** a more specific Thai message (e.g. what to do about the connector); falls back to ERROR_TEXT */
    public userText?: string,
  ) {
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

export const errorText = (e: SourceError) => e.userText ?? ERROR_TEXT[e.code];

/** why the owner's connector cannot be used right now (shown above every screen, demo data shown instead) */
export interface ConnectorIssue {
  /** the claude.ai mcp error code, or "no_host" / "no_mcp" when this view has no connector bridge */
  code: string;
  text: string;
  /** true when one more call from a click can ask the viewer for approval */
  approve: boolean;
  /** false when retrying from this view cannot help (e.g. the page is not open inside claude.ai) */
  retry: boolean;
}
