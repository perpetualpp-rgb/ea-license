# 🤖 Trading Agent Lab — BTC/USDT (Paper Trading)

A simulated, AI-agent trading lab inspired by the BoomBigNose video
*"เทรดอัตโนมัติด้วย AI Agent ใช้ Hermes และ Obsidian เชื่อมต่อ TradingView MCP แบบครบระบบ"*
and its [Setup Doc](https://docs.google.com/document/d/12n4JWBieXJmx6qbPWaYv70-7HGO-jJAqD6hGnLEoMkM/edit).

> ## ⚠️ PAPER TRADING ONLY
> This system **never connects to an exchange and never places a real order.**
> Every "trade" only updates an on-disk simulated portfolio. The `--live` flag
> and a `live_trading: true` config value are **intentionally refused.** Wiring
> a real broker is left entirely to you, at your own financial risk — automated
> live trading can lose money faster than you can react.

## What it does

Three agents compete on the same `BTCUSDT` feed, each with its own budget and
strategy (matching the Doc):

| Agent | Strategy | Budget | Logic |
|-------|----------|--------|-------|
| `Agent_01` | Trend Following | $33,333 | EMA12/EMA26 cross confirmed by MACD |
| `Agent_02` | Mean Reversion | $33,333 | Bollinger band / RSI extremes |
| `Agent_03` | AI-based Scalping | $33,334 | Price vs SMA20 momentum + MACD histogram |

Each cycle the lab:
1. Pulls a market snapshot — **real TradingView data by default** (falls back to
   the built-in simulated feed if offline; see *Data source* below).
2. Asks each agent for a `BUY` / `SELL` / `HOLD` signal. When an LLM is
   configured (Hermes by default), **each agent decides with the real model**
   using its own strategy persona; otherwise it uses the deterministic
   indicator rules (see *AI agents* below).
3. Applies the **consensus rule** — act only when **≥ 2 of 3** agents agree
   (the Doc's *"อย่างน้อย 2 ใน 3 เงื่อนไข"*).
4. Writes Obsidian-style Markdown notes to the vault.

## AI agents (Hermes / any OpenAI-compatible LLM)

Each of the three agents can decide with a **real LLM**. The agent sends its
strategy persona + the live indicator snapshot to the model and parses back a
`{"action", "reason"}` decision; the 2-of-3 consensus then runs on those AI
calls — i.e. **three independent AI agents per cycle**, as in the Doc.

Configured under `"llm"` in `config.json` (defaults target the Nous Research
Hermes inference API, OpenAI-compatible):

```json
"llm": {
  "enabled": true,
  "base_url": "https://inference-api.nousresearch.com/v1",
  "model": "Hermes-3-Llama-3.1-70B",
  "api_key_env": "NOUS_API_KEY"
}
```

Provide the key via the environment (never commit it):

```bash
export NOUS_API_KEY="sk-..."
python3 trading_analyzer.py --mode prod --once          # agents now use Hermes
python3 trading_analyzer.py --mode prod --once --no-llm  # force indicator rules
python3 trading_analyzer.py --mode prod --once --model Hermes-4-405B
```

**Fail-soft by design:** if `NOUS_API_KEY` is unset, the LLM is disabled, or any
network/parse error occurs, each agent transparently falls back to its
indicator rule — so the lab still runs fully offline. Point `base_url` /
`api_key_env` at OpenAI, OpenRouter, or a local server to use a different model.
The session log records whether each cycle was decided by AI or by rules.

## Browser dashboard (no install)

`index.html` is a self-contained dashboard that re-implements the whole lab in
JavaScript — open it directly (or via GitHub Pages at `/trading-agent-lab/`) to
watch the 3 agents, the 2-of-3 consensus, P/L and an Obsidian-style session log
update live. Same logic as the Python version, also paper-trading only.

> On load / reset / symbol change the dashboard **anchors to a real live price**
> (BTCUSDT from Binance's public API — real recent candles; XAUUSD from a free
> gold price API), then simulates movement from there. Decisions still use the
> **indicator rules**, not an LLM: a static page can't safely hold an API key,
> so the real TradingView feed and the real Hermes/LLM agents live only in the
> Python runner above. Run that (with `NOUS_API_KEY` set) for the genuine AI
> version. If the live fetch is blocked (CORS/offline) it falls back to a
> synthetic series seeded from a recent price.

## Components

| File | Role |
|------|------|
| `trading_analyzer.py` | Main CLI / orchestrator (the cronjob target) |
| `config.json` | Agents, symbol, timeframe, data source, consensus threshold |
| `market_data.py` | Snapshot provider (simulated or `tradingview-ta`) |
| `indicators.py` | Pure-Python SMA / EMA / RSI / MACD / Bollinger |
| `agents.py` | The three strategy agents |
| `decision.py` | 2-of-3 consensus logic |
| `paper_broker.py` | Simulated portfolio & order fills (the "go-live" seam) |
| `obsidian_logger.py` | Writes the vault's Markdown notes |

## Obsidian vault output (`vault/`)

- `trading-logs/Session_*.md` — one note per cycle (the Doc's Log Template)
- `Daily_Market_Summary.md` — market sentiment line (Knowledge Base checklist)
- `wallet_balance.md` — current paper balances (Risk Check)
- `last_trade_log.md` — heartbeat note (Cronjob Health)
- `state.json` — persisted price history + portfolios (runtime, git-ignored)

Point your Obsidian vault at the `vault/` directory to browse the notes live.

## Usage

The lab runs on the stdlib alone — no install needed. For the real data feed,
install `tradingview-ta`; for AI decisions, set `NOUS_API_KEY` (no package).

```bash
# Lab mode: each agent paper-trades its own budget; run 5 cycles locally
python3 trading_analyzer.py --mode lab --cycles 5 --interval 180

# Single cycle (intended for cron) — uses real TradingView data + Hermes if available
python3 trading_analyzer.py --mode prod --once

# Trade gold (XAUUSD) instead of BTCUSDT — keeps its own state/history
python3 trading_analyzer.py --mode prod --once --symbol XAUUSD

# Force the offline simulated feed and/or the indicator rules
python3 trading_analyzer.py --mode prod --once --source simulated --no-llm
```

### Data source

The default `source` in `config.json` is **`tradingview`** — best-effort real
analysis via the optional `tradingview-ta` package (mirroring the TradingView
MCP server). Install it with `pip install -r requirements.txt`. If the package
or network is unavailable, the snapshot transparently falls back to the
simulated feed and flags it (`source: simulated (tradingview unavailable)`).
Use `--source simulated` to force the offline feed.

### Symbols

Supported symbols live under `markets` in `config.json` (starting price +
TradingView screener/exchange). `BTCUSDT` and `XAUUSD` ship by default; add more
by extending that map. Each symbol keeps a separate `state_<SYMBOL>.json`. The
browser dashboard has a matching symbol dropdown.

### Cron (every 1 minute, as in the Doc)

```cron
* * * * * /usr/bin/python3 /path/to/trading-agent-lab/trading_analyzer.py --mode prod --once
```

### Run it automatically in the cloud (GitHub Actions)

`.github/workflows/trading-lab.yml` runs the lab on a schedule (~every 5 minutes,
GitHub's minimum) with **no machine of your own** — it installs `tradingview-ta`,
runs one cycle for BTCUSDT and XAUUSD, persists the vault between ticks via the
Actions cache, and uploads the Obsidian notes as a downloadable artifact.

One-time setup for real AI decisions: add a repository secret **`NOUS_API_KEY`**
(repo *Settings → Secrets and variables → Actions → New repository secret*).
Without it the scheduled run still works but falls back to the indicator rules.
You can also trigger it manually from the *Actions* tab (*Run workflow*).

> Note: GitHub's scheduled cron is best-effort — ticks may be delayed or skipped
> under load, so this is not a real-time 1-minute feed.

## Going live (not provided)

`paper_broker.py` is the single seam you would replace with a real exchange
client (e.g. ccxt). Doing so is deliberately out of scope here. If you build
it: start on a testnet, set hard risk limits, keep API keys in environment
variables (never in the repo), and require a human confirmation step before any
order. You are responsible for any real-money outcome.
