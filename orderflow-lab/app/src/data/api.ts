// Production adapter: talks only to the Flow Lab API (server/), never to exchanges or
// claude.ai connectors from the member's browser. Endpoints follow server/openapi.json.
// Until a server URL and sign-in exist, every call rejects with "not_configured".
import type { AlertEvent, Bar, Instrument, Lookback, MarketDetails, Observation, Quote, Series, Timeframe } from "./types";
import { SourceError, type MarketSource } from "./source";

const LOOKBACK_MS: Record<Lookback, number> = { "6h": 6 * 3600e3, "24h": 86400e3, "3d": 3 * 86400e3 };

export class ApiSource implements MarketSource {
  readonly mode = "api" as const;
  constructor(private baseUrl: string | null, private token: () => string | null = () => null) {}

  private async get<T>(path: string): Promise<T> {
    if (!this.baseUrl) throw new SourceError("not_configured", "no API base URL");
    const token = this.token();
    let res: Response;
    try {
      res = await fetch(this.baseUrl.replace(/\/$/, "") + path, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
    } catch {
      throw new SourceError("network", path);
    }
    if (res.status === 401 || res.status === 403) throw new SourceError("unauthorized", path);
    if (res.status === 429) throw new SourceError("rate_limited", path);
    if (res.status === 404) throw new SourceError("not_supported", path);
    if (!res.ok) throw new SourceError("server", `${res.status} ${path}`);
    return res.json() as Promise<T>;
  }

  instruments() {
    return this.get<Instrument[]>("/v1/instruments");
  }

  async series(id: string, tf: Timeframe, lookback: Lookback): Promise<Series> {
    const to = Date.now();
    const from = to - LOOKBACK_MS[lookback];
    const q = new URLSearchParams({ tf, from: new Date(from).toISOString(), to: new Date(to).toISOString() });
    return this.get<{ bars: Bar[] } & Omit<Series, "bars">>(`/v1/instruments/${encodeURIComponent(id)}/bars?${q}`);
  }

  // Not served by the API yet: reject with a reason the screens can show instead of guessing.
  quote(_id: string): Promise<Quote> {
    return Promise.reject(new SourceError("not_supported", "quote"));
  }
  observations(_id: string): Promise<Observation[]> {
    return Promise.reject(new SourceError("not_supported", "observations"));
  }
  details(_id: string, _tf: Timeframe, _lookback: Lookback): Promise<MarketDetails> {
    return Promise.reject(new SourceError("not_supported", "details"));
  }
  alertHistory(): Promise<AlertEvent[]> {
    return Promise.reject(new SourceError("not_supported", "alerts"));
  }
}
