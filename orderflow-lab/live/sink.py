"""Append-only storage: one JSON array per line, one file per stream per hour.

    <root>/<market>/<symbol>/<stream>/<YYYY-MM-DD>/<HH>.jsonl      (hour in progress)
    <root>/<market>/<symbol>/<stream>/<YYYY-MM-DD>/<HH>.jsonl.gz   (closed hours)
    <root>/manifest.jsonl                                          (one line per compression)

Hours are UTC and follow the timestamp the writer passes, which is when the
row reached the collector (the exchange event time for live trades, klines and
liquidations; the collector clock for book samples, meta rows and backfilled
trades). A row backfilled later therefore sits in a later file, possibly on a
later day: readers must use the timestamp inside the row (``row[0]``), read
neighbouring partitions, and dedupe.

Durability, honestly:

* Rows are flushed to the OS about every second and ``fsync``-ed every
  ``sync_every_s`` (5 s by default), before an hour file is closed, and on
  ``sync()``/``close()``. A power loss can lose the rows written since the
  last fsync (up to ~5 s of every stream); a plain crash of the process loses
  only what was still in Python's buffer (under ~1 s). The collector writes its
  checkpoint (last trade id, gap ledger) only after ``sync()``, so trades in
  the lost window are fetched again over REST; book samples, klines, mark,
  OI, liquidations and meta rows from that window are gone.
* A torn last line (crash mid-write) is skipped by ``read_rows`` and is
  newline-terminated before more rows are appended, so it never corrupts the
  next row.
* Compression is crash-safe: the new ``.gz`` is built in ``.gz.tmp`` (old
  members copied byte for byte, then the hour as a new gzip member) and
  fsync-ed, a manifest line is appended, the plain file is removed (the commit
  point) and the tmp renamed over the ``.gz``. ``recover()`` finishes or
  redoes an interrupted compression, so no hour is lost; at worst the
  manifest holds a stale extra line for a path (use the last line per path).
* Rows can be duplicated: trades fetched again after a restart, or a row
  re-sent after a reconnect. Dedupe by trade id / timestamp when reading.
"""
from __future__ import annotations

import glob
import gzip
import hashlib
import json
import os
import time
import zlib
from typing import Callable, Dict, Iterable, Iterator, List, Optional, TextIO, Tuple

MANIFEST = "manifest.jsonl"


def wall_ms() -> int:
    return int(time.time() * 1000)


def hour_path(root: str, market: str, symbol: str, stream: str, ts_ms: int) -> str:
    t = time.gmtime(ts_ms / 1000)
    return os.path.join(root, market, symbol, stream, time.strftime("%Y-%m-%d", t), time.strftime("%H", t) + ".jsonl")


def stream_files(root: str, market: str, symbol: str, stream: str, day: str = "*") -> List[str]:
    """Hour files of one stream (plain and gzipped, never ``.tmp``), sorted by day and hour."""
    base = os.path.join(root, market, symbol, stream, day)
    return sorted(glob.glob(os.path.join(base, "*.jsonl")) + glob.glob(os.path.join(base, "*.jsonl.gz")))


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return  # Windows cannot open a directory; NTFS journals the rename itself
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _row_stats(rows: Iterable[list], acc: Optional[dict] = None) -> dict:
    acc = acc or {"rows": 0, "first_ts": None, "last_ts": None}
    for r in rows:
        acc["rows"] += 1
        t = r[0] if isinstance(r, list) and r and isinstance(r[0], (int, float)) else None
        if t is not None:
            acc["first_ts"] = t if acc["first_ts"] is None else min(acc["first_ts"], t)
            acc["last_ts"] = t if acc["last_ts"] is None else max(acc["last_ts"], t)
    return acc


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _append_manifest(root: str, gz: str, tmp: str, stats: dict, closed_at: int) -> None:
    line = {"path": os.path.relpath(gz, root).replace(os.sep, "/"), "rows": stats["rows"], "bytes": os.path.getsize(tmp),
            "sha256": _sha256(tmp), "first_ts": stats["first_ts"], "last_ts": stats["last_ts"], "closed_at": closed_at}
    with open(os.path.join(root, MANIFEST), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def compress(path: str, root: Optional[str] = None, closed_at: Optional[int] = None) -> str:
    """Gzip a closed hour file in place (crash-safe order, see the module docstring).

    ``root`` given: append a manifest line describing the resulting ``.gz`` (rows,
    bytes, sha256 and min/max ``row[0]`` over all its members)."""
    gz, tmp = path + ".gz", path + ".gz.tmp"
    stats = _row_stats(read_rows(gz)) if os.path.exists(gz) else _row_stats(())
    with open(tmp, "wb") as out:
        if os.path.exists(gz):
            with open(gz, "rb") as old:  # gzip members concatenate: keep the old bytes as they are
                for chunk in iter(lambda: old.read(1 << 20), b""):
                    out.write(chunk)
        with open(path, "rb") as src, gzip.GzipFile(filename="", mode="wb", fileobj=out, compresslevel=6, mtime=0) as dst:
            for line in src:
                dst.write(line if line.endswith(b"\n") else line + b"\n")  # never glue a torn line to the next member
        out.flush()
        os.fsync(out.fileno())
    _row_stats(read_rows(path), stats)
    if root is not None:
        _append_manifest(root, gz, tmp, stats, closed_at if closed_at is not None else wall_ms())
    d = os.path.dirname(path)
    os.remove(path)  # commit point: from here recover() completes the rename
    _fsync_dir(d)
    os.replace(tmp, gz)
    _fsync_dir(d)
    return gz


def _finish_tmp(tmp: str) -> None:
    plain = tmp[: -len(".gz.tmp")]
    if os.path.exists(plain):
        os.remove(tmp)  # interrupted before the commit point: the plain file is still the truth
    else:
        os.replace(tmp, plain + ".gz")  # interrupted after it: the tmp is complete and fsync-ed
        _fsync_dir(os.path.dirname(tmp))


class HourlySink:
    def __init__(self, root: str, clock: Optional[Callable[[], int]] = None, sync_every_s: float = 5.0):
        self.root = root
        self.clock = clock or wall_ms
        self.sync_every_s = sync_every_s
        self.open: Dict[Tuple[str, str, str], Tuple[str, TextIO]] = {}
        self.rows = 0
        self.last_flush = self.last_sync = time.monotonic()

    def compress(self, path: str) -> str:
        return compress(path, self.root, self.clock())

    def recover(self, now_ms: Optional[int] = None) -> int:
        """Finish interrupted compressions, then compress plain hour files that are
        not the current hour (left by a crash or stop). Returns how many were compressed."""
        now_ms = self.clock() if now_ms is None else now_ms
        current = time.strftime("%Y-%m-%d/%H", time.gmtime(now_ms / 1000))
        tmps, plains = [], []
        for d, _, files in os.walk(self.root):
            for f in files:
                if f.endswith(".jsonl.gz.tmp"):
                    tmps.append(os.path.join(d, f))
                elif f.endswith(".jsonl") and f != MANIFEST:
                    plains.append(os.path.join(d, f))
        for p in tmps:
            _finish_tmp(p)
        done = 0
        for p in sorted(plains):
            if os.path.basename(os.path.dirname(p)) + "/" + os.path.basename(p)[:-6] != current and os.path.exists(p):
                self.compress(p)
                done += 1
        return done

    def write(self, market: str, symbol: str, stream: str, ts_ms: int, row: list) -> None:
        key = (market, symbol, stream)
        path = hour_path(self.root, market, symbol, stream, ts_ms)
        cur = self.open.get(key)
        if cur is None or cur[0] != path:
            if cur is not None:
                self._close(cur[1])
                if os.path.exists(cur[0]) and cur[0] < path:
                    self.compress(cur[0])  # the hour rolled over
            cur = (path, self._open(path))
            self.open[key] = cur
        cur[1].write(json.dumps(row, separators=(",", ":")) + "\n")
        self.rows += 1

    @staticmethod
    def _open(path: str) -> TextIO:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        torn = False
        if os.path.exists(path) and os.path.getsize(path):
            with open(path, "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                torn = fh.read(1) != b"\n"
        fh = open(path, "a", encoding="utf-8")
        if torn:
            fh.write("\n")  # end the torn line so the next row starts clean
        return fh

    @staticmethod
    def _close(fh: TextIO) -> None:
        fh.flush()
        os.fsync(fh.fileno())
        fh.close()

    def flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self.last_flush >= 1.0:
            for _, fh in self.open.values():
                fh.flush()
            self.last_flush = now
        if force or now - self.last_sync >= self.sync_every_s:
            for _, fh in self.open.values():
                fh.flush()
                os.fsync(fh.fileno())
            self.last_sync = now

    def sync(self) -> None:
        """Make every row written so far durable (flush + fsync)."""
        self.flush(force=True)

    def close(self) -> None:
        for _, fh in self.open.values():
            self._close(fh)
        self.open.clear()


def read_rows(path: str) -> Iterator[list]:
    """Read one hour file (plain or gzipped). A torn line from a crash is skipped; a
    truncated or corrupt gzip tail ends the file instead of raising."""
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except (EOFError, zlib.error, gzip.BadGzipFile):
        return


def read_manifest(root: str) -> Dict[str, dict]:
    """Latest manifest line per path (an interrupted compression can leave an earlier one)."""
    out: Dict[str, dict] = {}
    p = os.path.join(root, MANIFEST)
    if not os.path.exists(p):
        return out
    for row in read_rows(p):
        if isinstance(row, dict) and "path" in row:
            out[row["path"]] = row
    return out
