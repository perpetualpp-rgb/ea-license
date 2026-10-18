#!/usr/bin/env python3
"""Coverage report for collected data: is anything missing?

    python3 -m live.check                  # yesterday and today (UTC)
    python3 -m live.check --day 2026-10-18

Per symbol and day:
* trades  share of aggTrade ids present between the first and last id seen
* book    share of seconds with a book row, between the first and last row
* kline   share of minutes with a closed candle
* events  reconnects, book resyncs and trade ids that could not be recovered
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(HERE)
if LAB not in sys.path:
    sys.path.insert(0, LAB)

from live.sink import read_rows  # noqa: E402


def rows_for(root: str, market: str, symbol: str, stream: str, day: str):
    for path in sorted(glob.glob(os.path.join(root, market, symbol, stream, day, "*.jsonl*"))):
        yield from read_rows(path)


def coverage(root: str, market: str, symbol: str, day: str) -> dict:
    ids = sorted({r[1] for r in rows_for(root, market, symbol, "trades", day)})
    trades = {"rows": len(ids), "pct": None}
    if ids:
        span = ids[-1] - ids[0] + 1
        trades["pct"] = 100.0 * len(ids) / span
    secs = sorted({r[0] // 1000 for r in rows_for(root, market, symbol, "book", day)})
    book = {"rows": len(secs), "pct": 100.0 * len(secs) / (secs[-1] - secs[0] + 1) if secs else None}
    mins = sorted({r[0] // 60000 for r in rows_for(root, market, symbol, "kline", day)})
    kline = {"rows": len(mins), "pct": 100.0 * len(mins) / (mins[-1] - mins[0] + 1) if mins else None}
    ev = {"connected": 0, "disconnected": 0, "book_resync": 0, "trade_gap": 0, "lost_ids": 0}
    for r in rows_for(root, market, symbol, "meta", day):
        if r[1] in ev:
            ev[r[1]] += 1
        if r[1] == "trade_gap":
            ev["lost_ids"] += r[3] - r[2] + 1
    return {"trades": trades, "book": book, "kline": kline, "events": ev}


def fmt_pct(x):
    return "–" if x is None else f"{x:6.2f}%"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="coverage of collected data")
    ap.add_argument("--data", default=os.path.join(LAB, "data", "live"))
    ap.add_argument("--day", action="append", help="UTC day YYYY-MM-DD (repeatable)")
    args = ap.parse_args(argv)
    days = args.day or [time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400)), time.strftime("%Y-%m-%d", time.gmtime())]
    found = False
    for mdir in sorted(glob.glob(os.path.join(args.data, "*", "*"))):
        if not os.path.isdir(mdir):
            continue
        market, symbol = mdir.split(os.sep)[-2:]
        for day in days:
            c = coverage(args.data, market, symbol, day)
            if not (c["trades"]["rows"] or c["book"]["rows"] or c["kline"]["rows"]):
                continue
            found = True
            e = c["events"]
            print(f"{day} {market:8s} {symbol:9s} trades {c['trades']['rows']:>9,} {fmt_pct(c['trades']['pct'])}"
                  f" | book {c['book']['rows']:>6,} s {fmt_pct(c['book']['pct'])} | kline {c['kline']['rows']:>5} {fmt_pct(c['kline']['pct'])}"
                  f" | reconnects {e['disconnected']} resyncs {e['book_resync']} lost ids {e['lost_ids']}")
    if not found:
        print("no data for", ", ".join(days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
