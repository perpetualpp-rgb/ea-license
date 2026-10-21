#!/usr/bin/env python3
"""Print an order-flow summary from collected data or a bar file.

    python3 report.py --source okx:BTC-USDT                 # trades + books (full)
    python3 report.py --source kraken:PAXGUSD --window-min 30
    python3 report.py --bars my_bars.jsonl --tick 0.1 --round-step 25   # any {t,o,h,l,c,v} JSONL
    python3 report.py --source okx:BTC-USDT --json           # machine-readable
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from collector import file_stem, load_config
from engine.summary import summarize_bars, summarize_trades
from feeds.types import read_bars, read_books, read_trades

HERE = os.path.dirname(os.path.abspath(__file__))


def fmt_ts(ms_or_s) -> str:
    if ms_or_s is None:
        return "-"
    s = ms_or_s / 1000 if ms_or_s > 10_000_000_000 else ms_or_s
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(s)) + " UTC"


def render(r: dict, label: str) -> str:
    if "error" in r:
        return f"{label}: {r['error']}"
    approx = " (approximate: bars only)" if r.get("approximate") else ""
    L = [f"== {label}{approx}", f"last={r['last']}  window {fmt_ts(r.get('ts_first', r.get('t_first')))} -> {fmt_ts(r.get('ts_last', r.get('t_last')))}"]
    if r["mode"] == "trades":
        L.append(f"trades={r['trades']}  buy={r['profile_buy']:.4f}  sell={r['profile_sell']:.4f}")
    rg = r["range"]
    L.append(f"range  high={rg['high']} low={rg['low']} POC={rg['poc']}  value area {int(rg['va_pct']*100)}%: {rg['va_low']}..{rg['va_high']}")
    m = r["magnet"]
    L.append(f"magnet {int(m['pct']*100)}%: {m['low']}..{m['high']}   HVN={[n['price'] for n in r['nodes']['hvn']]}  LVN={[n['price'] for n in r['nodes']['lvn']]}")
    v = r.get("vwap")
    if v:
        b2 = v["bands"][2.0]
        L.append(f"vwap={v['vwap']:.2f} sd={v['sd']:.2f} ±2σ=[{b2[0]:.2f}, {b2[1]:.2f}]  price is {v['position']['label']} (z={v['position']['z']:+.2f})  bands={v['shape']}")
    c = r["cvd"]
    tag = "≈" if c["approximate"] else ""
    L.append(f"CVD{tag}={c['value']:+.4f}  divergence={c['divergence']}")
    if "last_buckets" in c:
        L.append("delta/bucket: " + "  ".join(f"{fmt_ts(b['t'])[11:16]} {b['delta']:+.4f}" for b in c["last_buckets"]))
    L.append(f"round levels {r['round_levels']}  nearest={r['nearest_round']}")
    if "big_trades" in r:
        bt = r["big_trades"]
        L.append(f"big trades: threshold(P{int(bt['pct']*100)})={bt['threshold']:.4f} count={bt['count']}")
        for z in bt["zones"][:5]:
            flag = " CONTESTED" if z["contested"] else ""
            L.append(f"   {z['price']}: buy={z['buy']:.4f} sell={z['sell']:.4f} n={z['n']}{flag}")
    if "walls" in r:
        w = r["walls"]
        L.append(f"walls from {w['snapshots']} snapshots  best {w['best_bid']}/{w['best_ask']} @ {fmt_ts(w['book_ts'])}")
        for side in ("ask", "bid"):
            for x in w[side]:
                p = " persistent" if x["persistent"] else ""
                L.append(f"   {side} {x['price']} size={x['size']:.4f} x{x['mult']:.1f} dist={x['dist_pct']:.2f}% {x['status']} seen={x['seen']} age={x['age_ms']//1000}s growth={x['growth']:+.0%}{p}")
        for e in w["events"][-5:]:
            L.append(f"   event {e['side']} {e['price']} size={e['size']:.4f} -> {e['outcome']} after {e['age_ms']//1000}s")
        if w["events"]:
            L.append("   (outcome = where price was when the wall left the book: price_reached / removed / unknown;"
                     " L2 cannot tell a fill from a cancel)")
    return "\n".join(L)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=os.path.join(HERE, "config.json"))
    p.add_argument("--data", default=os.path.join(HERE, "data"))
    p.add_argument("--source", help="exchange:symbol from config.json")
    p.add_argument("--bars", help="bar JSONL file (candle-only mode)")
    p.add_argument("--tick", type=float, help="profile bucket size (default from config)")
    p.add_argument("--round-step", type=float, help="round-number step (default from config)")
    p.add_argument("--window-min", type=float, default=60.0, help="trades window in minutes")
    p.add_argument("--interval", default="1m", help="bucket interval for delta/VWAP: 1m 5m 15m")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    cfg = load_config(args.config)
    eng = cfg.get("engine", {})
    interval_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}[args.interval]

    if args.bars:
        bars = read_bars(args.bars)
        tick = args.tick or cfg.get("bars", {}).get("tick", 0.1)
        step = args.round_step or cfg.get("bars", {}).get("round_step", 25)
        r = summarize_bars(bars, tick, step, eng.get("va_pct", 0.70), eng.get("magnet_pct", 0.30))
        label = os.path.basename(args.bars)
    elif args.source:
        src = next((s for s in cfg["sources"] if f"{s['exchange']}:{s['symbol']}" == args.source), None)
        if not src:
            print(f"unknown source {args.source}", file=sys.stderr)
            return 2
        stem = os.path.join(args.data, file_stem(src))
        trades = read_trades(stem + "_trades.jsonl") if os.path.exists(stem + "_trades.jsonl") else []
        books = read_books(stem + "_book.jsonl") if os.path.exists(stem + "_book.jsonl") else []
        if trades:
            cutoff = trades[-1].ts - int(args.window_min * 60_000)
            trades = [t for t in trades if t.ts >= cutoff]
            books = [b for b in books if b.ts >= cutoff]
        r = summarize_trades(trades, books, args.tick or src["tick"], args.round_step or src["round_step"], interval_ms,
                             eng.get("va_pct", 0.70), eng.get("magnet_pct", 0.30), eng.get("big_pct", 0.99),
                             src.get("big_min", 0.0), eng.get("wall_mult", 4.0), eng.get("wall_depth", 200),
                             eng.get("wall_persist_s", 60) * 1000, eng.get("wall_max_dist_pct", 1.0))
        label = f"{src.get('label', args.source)} ({args.source})"
    else:
        p.error("give --source or --bars")
        return 2
    print(json.dumps(r, indent=1, default=str) if args.json else render(r, label))
    return 0


if __name__ == "__main__":
    sys.exit(main())
