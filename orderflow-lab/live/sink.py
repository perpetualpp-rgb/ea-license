"""Append-only storage: one JSON array per line, one file per stream per hour.

    <root>/<market>/<symbol>/<stream>/<YYYY-MM-DD>/<HH>.jsonl      (hour in progress)
    <root>/<market>/<symbol>/<stream>/<YYYY-MM-DD>/<HH>.jsonl.gz   (closed hours)

The hour in progress stays plain text and is flushed every second, so a crash
loses at most a second. When the hour changes the file is gzipped in place;
on start, plain files left from earlier hours are compressed too. Hours are
UTC and follow the time a row arrived (the exchange event time), so a row
backfilled later sits in a later file: readers sort by the timestamp inside.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import time
from typing import Dict, Iterator, Optional, TextIO, Tuple


def hour_path(root: str, market: str, symbol: str, stream: str, ts_ms: int) -> str:
    t = time.gmtime(ts_ms / 1000)
    return os.path.join(root, market, symbol, stream, time.strftime("%Y-%m-%d", t), time.strftime("%H", t) + ".jsonl")


def compress(path: str) -> str:
    gz = path + ".gz"
    if os.path.exists(gz):
        # a late row reopened a closed hour: add it as another gzip member
        with open(path, "rb") as src, gzip.open(gz, "ab", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
    else:
        with open(path, "rb") as src, gzip.open(gz + ".tmp", "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        os.replace(gz + ".tmp", gz)
    os.remove(path)
    return gz


class HourlySink:
    def __init__(self, root: str):
        self.root = root
        self.open: Dict[Tuple[str, str, str], Tuple[str, TextIO]] = {}
        self.rows = 0
        self.last_flush = time.monotonic()

    def recover(self, now_ms: Optional[int] = None) -> int:
        """Compress plain hour files that are not the current hour (left by a crash or stop)."""
        now_ms = now_ms or int(time.time() * 1000)
        current = time.strftime("%Y-%m-%d/%H", time.gmtime(now_ms / 1000))
        done = 0
        for d, _, files in os.walk(self.root):
            for f in files:
                if f.endswith(".jsonl"):
                    p = os.path.join(d, f)
                    tag = os.path.basename(d) + "/" + f[:-6]
                    if tag != current:
                        compress(p)
                        done += 1
        return done

    def write(self, market: str, symbol: str, stream: str, ts_ms: int, row: list) -> None:
        key = (market, symbol, stream)
        path = hour_path(self.root, market, symbol, stream, ts_ms)
        cur = self.open.get(key)
        if cur is None or cur[0] != path:
            if cur is not None:
                cur[1].close()
                if os.path.exists(cur[0]) and cur[0] < path:
                    compress(cur[0])  # the hour rolled over
            os.makedirs(os.path.dirname(path), exist_ok=True)
            cur = (path, open(path, "a", encoding="utf-8"))
            self.open[key] = cur
        cur[1].write(json.dumps(row, separators=(",", ":")) + "\n")
        self.rows += 1

    def flush(self, force: bool = False) -> None:
        if force or time.monotonic() - self.last_flush >= 1.0:
            for _, fh in self.open.values():
                fh.flush()
            self.last_flush = time.monotonic()

    def close(self) -> None:
        for _, fh in self.open.values():
            fh.close()
        self.open.clear()


def read_rows(path: str) -> Iterator[list]:
    """Read one hour file (plain or gzipped); a torn last line from a crash is skipped."""
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
