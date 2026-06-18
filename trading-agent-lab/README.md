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
1. Pulls a market snapshot (simulated feed by default; optional real data via
   TradingView).
2. Asks each agent for a `BUY` / `SELL` / `HOLD` signal.
3. Applies the **consensus rule** — act only when **≥ 2 of 3** agents agree
   (the Doc's *"อย่างน้อย 2 ใน 3 เงื่อนไข"*).
4. Writes Obsidian-style Markdown notes to the vault.

## Browser dashboard (no install)

`index.html` is a self-contained dashboard that re-implements the whole lab in
JavaScript — open it directly (or via GitHub Pages at `/trading-agent-lab/`) to
watch the 3 agents, the 2-of-3 consensus, P/L and an Obsidian-style session log
update live. Same logic as the Python version, also paper-trading only.

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

No install needed for the default simulated mode (stdlib only):

```bash
# Lab mode: each agent paper-trades its own budget; run 5 cycles locally
python3 trading_analyzer.py --mode lab --cycles 5 --interval 180

# Single cycle (intended for cron)
python3 trading_analyzer.py --mode prod --once

# Use real TradingView data instead of the simulated feed
pip install -r requirements.txt
python3 trading_analyzer.py --mode prod --once --source tradingview
```

### Cron (every 1 minute, as in the Doc)

```cron
* * * * * /usr/bin/python3 /path/to/trading-agent-lab/trading_analyzer.py --mode prod --once
```

## Going live (not provided)

`paper_broker.py` is the single seam you would replace with a real exchange
client (e.g. ccxt). Doing so is deliberately out of scope here. If you build
it: start on a testnet, set hard risk limits, keep API keys in environment
variables (never in the repo), and require a human confirmation step before any
order. You are responsible for any real-money outcome.
