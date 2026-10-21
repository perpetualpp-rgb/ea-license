#!/usr/bin/env python3
"""Poll public exchange APIs and append normalised trades/books to JSONL.

    python3 collector.py --once                 # one poll of every source
    python3 collector.py --loop --interval 5    # keep polling (Ctrl-C to stop)
    python3 collector.py --once --only okx:BTC-USDT

Output per source (under ``--data``, default ``data/``):
    <exchange>_<symbol>_trades.jsonl   one Trade per line (taker side)
    <exchange>_<symbol>_book.jsonl     one truncated BookSnapshot per poll
    state.json                         dedupe cursors so restarts don't double-count
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import List

from engine.summary import one_liner
from feeds.registry import make_feed
from feeds.types import Trade, to_json

HERE = os.path.dirname(os.path.abspath(__file__))
SEEN_KEEP = 5000  # trade ids remembered per source for de-duplication


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def source_key(cfg: dict) -> str:
    return f"{cfg['exchange']}:{cfg['symbol']}"


def file_stem(cfg: dict) -> str:
    return f"{cfg['exchange']}_{cfg['symbol']}".replace("/", "-")


class Source:
    def __init__(self, cfg: dict, data_dir: str, state: dict):
        self.cfg = cfg
        self.key = source_key(cfg)
        self.label = cfg.get("label", self.key)
        self.feed = make_feed(cfg)
        self.trades_path = os.path.join(data_dir, f"{file_stem(cfg)}_trades.jsonl")
        self.book_path = os.path.join(data_dir, f"{file_stem(cfg)}_book.jsonl")
        self.state = state.setdefault(self.key, {"seen_ids": [], "last_ts": 0})
        if hasattr(self.feed, "cursor"):  # kraken: resume from the stored cursor
            self.feed.cursor = self.state.get("cursor")

    def poll(self, depth: int) -> tuple[List[Trade], object]:
        seen = set(self.state["seen_ids"])
        fresh = [t for t in self.feed.fetch_trades() if t.trade_id not in seen]
        if fresh:
            with open(self.trades_path, "a", encoding="utf-8") as fh:
                fh.writelines(to_json(t) + "\n" for t in fresh)
            self.state["last_ts"] = fresh[-1].ts
            self.state["seen_ids"] = (self.state["seen_ids"] + [t.trade_id for t in fresh])[-SEEN_KEEP:]
        if hasattr(self.feed, "cursor"):
            self.state["cursor"] = self.feed.cursor
        book = self.feed.fetch_book(depth).truncated(depth)
        with open(self.book_path, "a", encoding="utf-8") as fh:
            fh.write(to_json(book) + "\n")
        return fresh, book


def run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    os.makedirs(args.data, exist_ok=True)
    state_path = os.path.join(args.data, "state.json")
    state = json.load(open(state_path, encoding="utf-8")) if os.path.exists(state_path) else {}
    only = set(args.only.split(",")) if args.only else None
    sources = [Source(s, args.data, state) for s in cfg["sources"] if not only or source_key(s) in only]
    if not sources:
        print("no sources selected", file=sys.stderr)
        return 2
    depth = args.depth or cfg.get("collector", {}).get("book_depth", 200)
    interval = args.interval or cfg.get("collector", {}).get("interval_s", 5)

    while True:
        t0 = time.time()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        for src in sources:
            try:
                fresh, book = src.poll(depth)
                print(f"{stamp}  {one_liner(src.label, fresh, book, cfg.get('engine', {}).get('wall_mult', 4.0))}")
            except Exception as exc:  # keep polling the other sources
                print(f"{stamp}  {src.label:<14} ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        if not args.loop:
            return 0
        time.sleep(max(0.0, interval - (time.time() - t0)))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=os.path.join(HERE, "config.json"))
    p.add_argument("--data", default=os.path.join(HERE, "data"))
    p.add_argument("--only", help="comma-separated exchange:symbol keys to poll")
    p.add_argument("--depth", type=int, help="book levels per side to store")
    p.add_argument("--interval", type=float, help="seconds between polls in --loop mode")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    args = p.parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
