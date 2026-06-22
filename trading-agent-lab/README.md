# 🤖 Trading Agent Lab — BTC/USDT (Paper Trading)

A simulated, AI-agent trading lab inspired by the BoomBigNose video
*"เทรดอัตโนมัติด้วย AI Agent ใช้ Hermes และ Obsidian เชื่อมต่อ TradingView MCP แบบครบระบบ"*
and its [Setup Doc](https://docs.google.com/document/d/12n4JWBieXJmx6qbPWaYv70-7HGO-jJAqD6hGnLEoMkM/edit).

> ## ⚠️ PAPER BY DEFAULT — LIVE IS OPT-IN, AT YOUR OWN RISK
> Out of the box this system **paper-trades only**: every "trade" just updates
> an on-disk simulated portfolio. Real-money trading exists (`live_broker.py`)
> but is **off behind several independent gates** and defaults to
> *testnet + dry-run*. Turning it on means real orders with real money, which
> can lose money faster than you can react. **Start on testnet, set hard risk
> limits, and you are solely responsible for any financial outcome.** See
> *Going live* below.

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

> The browser dashboard stays on the **simulated feed + indicator rules** on
> purpose: a static page can't safely hold an API key, so real TradingView data
> and the real Hermes/LLM agents live only in the Python runner above. Run that
> (with `NOUS_API_KEY` set) for the genuine AI version.

## Flows Agent (declarative node pipeline)

On top of the fixed `trading_analyzer.py` orchestration there is a **Flows
Agent** — the *"Flows Agent"* idea (inspired by the [Flows Agent
clip](https://www.facebook.com/share/r/17qA2JQ68b) and the quant patterns in
[`javajack/skill-algotrader`](https://github.com/javajack/skill-algotrader)):
express a strategy as a **wired graph of small, observable nodes** declared in
`flow.json`, not as hard-coded control flow. Reorder, add, or drop nodes to
change the strategy without touching Python.

The default flow:

```
market_data → strategy_agents → fortress → consensus → risk → execute → obsidian_log
```

1. **market_data** — snapshot + ATR / trend-strength proxies.
2. **strategy_agents** — the 3 lab agents vote (LLM or rules).
3. **fortress** — a *multi-factor confirmation* vote ported from
   `skill-algotrader`'s "Fortress" signal: only act when trend strength **and**
   RSI zone **and** momentum all agree (its ADX/volume inputs become close-only
   proxies — see `fortress.py`).
4. **consensus** — collapse all votes via the N-of-M majority rule.
5. **risk** — size the order with **regime-aware fractional Kelly** + a
   consecutive-loss throttle (`risk.py`, also from the skill).
6. **execute** — paper-trade the sized decision (no real orders, ever).
7. **obsidian_log** — the usual vault notes, plus a per-run trace under
   `vault/flow-runs/` so every node's output is auditable.

```bash
python3 flow.py --once                               # one cycle (cron target)
python3 flow.py --cycles 5 --interval 180            # local self-loop
python3 flow.py --once --source simulated --no-llm   # fully offline
python3 flow.py --once --symbol XAUUSD               # trade gold instead
python3 flow.py --flow my_flow.json --once           # your own node graph
```

`flow.py` shares `config.json`, the data feed, the LLM client and the paper
broker with the analyzer, and refuses `--live` / `live_trading: true` the same
way. The `fortress` and `risk` blocks in `config.json` tune those two nodes.

## Components

| File | Role |
|------|------|
| `trading_analyzer.py` | Fixed CLI / orchestrator (the original cronjob target) |
| `flow.py` | **Flows Agent** — declarative node-pipeline runner |
| `flow.json` | The node graph the Flows Agent executes |
| `flow_nodes.py` | Node library (data / agents / fortress / consensus / risk / execute / log) |
| `fortress.py` | Multi-factor "Fortress" confirmation signal (from `skill-algotrader`) |
| `risk.py` | Regime detection + fractional-Kelly position sizing |
| `config.json` | Agents, symbol, timeframe, data source, consensus, fortress, risk |
| `market_data.py` | Snapshot provider (simulated or `tradingview-ta`) |
| `indicators.py` | Pure-Python SMA / EMA / RSI / MACD / Bollinger / ATR / trend-strength |
| `agents.py` | The three strategy agents |
| `decision.py` | N-of-M consensus logic |
| `paper_broker.py` | Simulated portfolio & order fills |
| `broker.py` | Broker selector + live-trading gates + daily-loss kill-switch |
| `live_broker.py` | Real-exchange orders via ccxt (opt-in; testnet + dry-run by default) |
| `obsidian_logger.py` | Writes the vault's Markdown notes |
| `Dockerfile` / `docker-compose.yml` / `.env.example` | Deploy as a long-running service |

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

## Run it for real (real data + real AI, still paper)

The lab is built to run for real, not just demo:

* **Real market data** — the default `source` is `tradingview`; install the
  feed with `pip install -r requirements.txt` and snapshots come from live
  TradingView analysis (falls back to the simulated feed if offline).
* **Real AI agents** — set `NOUS_API_KEY` (or point `llm.base_url` at any
  OpenAI-compatible endpoint) and the three agents + you get genuine model
  decisions, with a transparent rule-based fallback.
* **Deploy it** — run continuously without touching real money:

```bash
cp .env.example .env            # add NOUS_API_KEY; leave ALLOW_LIVE=0
docker compose up -d            # paper-trades every 180s, vault persisted to ./vault
# or, with cron, one cycle per minute:
* * * * * cd /path/to/trading-agent-lab && /usr/bin/python3 flow.py --once
```

`config.json` ships with `broker.mode: "paper"`, so all of the above stays
paper-trading until you deliberately go live.

## Going live (real orders — opt-in, gated)

Live trading is implemented in `live_broker.py` via **ccxt** (Binance + 100+
exchanges, with testnet/sandbox). It is the single seam that places real
orders, and it only fires when **every** gate is satisfied — otherwise it falls
back to paper or dry-run:

| Gate | Where | Default |
|------|-------|---------|
| `live_trading: true` | `config.json` (master switch) | `false` |
| `broker.mode: "live"` | `config.json` | `"paper"` |
| `--live` flag / `ALLOW_LIVE=1` | per invocation | off |
| `EXCHANGE_API_KEY` + `EXCHANGE_API_SECRET` | environment | unset |
| `broker.dry_run: false` | `config.json` | `true` (log-only) |
| `broker.testnet` | `config.json` | `true` (sandbox venue) |
| per-order `max_order_usd` cap + `max_daily_loss_usd` kill-switch | `config.json` | `$50` / `$100` |

Recommended path to first real order:

```bash
# 1) Dry-run on testnet first — proves the wiring, sends nothing.
#    config.json: live_trading=true, broker.mode=live (keep dry_run=true, testnet=true)
python3 flow.py --once --live

# 2) Testnet for real — set broker.dry_run=false, use TESTNET keys.
EXCHANGE_API_KEY=... EXCHANGE_API_SECRET=... ALLOW_LIVE=1 python3 flow.py --once --live

# 3) Live venue — only once you trust it: broker.testnet=false, real keys,
#    small max_order_usd. The runner prints a loud banner before any real order.
```

Keep keys in the environment (never the repo — `.env` is git-ignored), keep
`max_order_usd` small, and watch the daily-loss kill-switch in
`vault/state_*.json`. **You are responsible for any real-money outcome.**
