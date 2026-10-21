#!/usr/bin/env python3
"""Coverage report for collected data: what is missing, measured against the clock.

    python3 -m live.check                  # yesterday and today (UTC)
    python3 -m live.check --day 2026-10-18

Per market, symbol and UTC day, each measure is reported separately:

* window   the day, clipped to "now" for today; a day wholly in the future has none
* book     distinct seconds with a book sample / seconds in the window, plus the
           holes of 60 s or more (collector down, book resyncing)
* kline    distinct closed 1-minute candles DELIVERED BY THE WEBSOCKET / minutes that
           had closed in the window. Binance sends a closed candle every minute even
           when nothing traded, so this is the best liveness measure of the WebSocket.
           Candles the collector fetched over REST (start-up history, reconnect
           backfill: the minutes named by ``kline_history`` / ``kline_backfill`` meta
           rows) are left out of it and reported apart: ``rest_minutes``, and
           ``complete_pct`` = all candles on disk / minutes in the window (what a
           chart has). ``lost_minutes`` = minutes in ``kline_gap`` rows still absent
* trades   id continuity = ids present / (max - min + 1) among the ids seen that
           day. It only says whether the observed span has holes; it is NOT uptime
           (a collector that ran one minute scores 100 %)
* fresh    age of the newest row of each stream at report time
* events   from the meta stream: connects, disconnects, book resyncs, backfill
           retries and trade ids recorded as unrecoverable

``None`` (printed N/A) means there is no evidence, never "complete": a day with no
rows at all is N/A, while a stream with no rows on a day where other streams of
the same symbol have rows is 0 %. REST candles alone are not evidence that the
collector ran that day (a fresh start fills the day before). Rows are filed
by the time they reached the collector, so a backfilled row can sit in a later
day's files: the report reads the day before and up to ``late_days`` after, and
assigns every row by the event timestamp inside it (``row[0]``).
"""
from __future__ import annotations

import argparse
import calendar
import glob
import os
import sys
import time
from typing import Iterator, List, Optional, Set, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(HERE)
if LAB not in sys.path:
    sys.path.insert(0, LAB)

from live.sink import read_rows, stream_files  # noqa: E402

DAY_MS, MIN_MS = 86_400_000, 60_000
HOLE_S = 60  # book holes shorter than this are not listed (they still count against coverage)
EVENTS = ("connected", "disconnected", "book_resync", "book_sync_failed", "snapshot_error", "backfill_retry", "backfill", "trade_gap",
          "kline_history", "kline_backfill", "kline_retry", "kline_gap")
KLINE_REST = ("kline_history", "kline_backfill")  # meta rows naming the minutes the collector wrote from REST


def day_start(day: str) -> int:
    return calendar.timegm(time.strptime(day, "%Y-%m-%d")) * 1000


def day_name(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def event_rows(root: str, market: str, symbol: str, stream: str, day: str, late_days: int = 1) -> Iterator[list]:
    """Rows whose own timestamp (row[0]) falls on ``day``, from that day's files and its neighbours'."""
    lo = day_start(day)
    hi = lo + DAY_MS
    for k in range(-1, late_days + 1):
        for path in stream_files(root, market, symbol, stream, day_name(lo + k * DAY_MS)):
            for r in read_rows(path):
                if isinstance(r, list) and r and isinstance(r[0], (int, float)) and lo <= r[0] < hi:
                    yield r


def kline_spans(root: str, market: str, symbol: str, day: str, late_days: int = 1) -> Tuple[Set[int], Set[int]]:
    """Minutes (open_ms // MIN_MS) of ``day`` that the collector wrote from REST, and those it recorded
    as ``kline_gap``. A meta row is written when the candles were fetched (that day or later), so the
    day's and the next ``late_days`` days' meta files are read whatever the row's own timestamp."""
    first = day_start(day) // MIN_MS
    last = first + DAY_MS // MIN_MS - 1
    rest: Set[int] = set()
    gap: Set[int] = set()
    for k in range(0, late_days + 1):
        for path in stream_files(root, market, symbol, "meta", day_name(day_start(day) + k * DAY_MS)):
            for r in read_rows(path):
                if not (isinstance(r, list) and len(r) >= 4 and r[1] in KLINE_REST + ("kline_gap",)):
                    continue
                try:
                    a, b = int(r[2]) // MIN_MS, int(r[3]) // MIN_MS
                except (TypeError, ValueError):
                    continue
                (rest if r[1] in KLINE_REST else gap).update(range(max(a, first), min(b, last) + 1))
    return rest, gap


def window(day: str, now_ms: int) -> Optional[Tuple[int, int]]:
    lo = day_start(day)
    hi = min(lo + DAY_MS, now_ms)
    return (lo, hi) if hi > lo else None


def pct(have: int, expected: int, evidence: bool) -> Optional[float]:
    return 100.0 * have / expected if evidence and expected > 0 else None


def holes(secs: List[int], lo_s: int, hi_s: int, min_s: int = HOLE_S) -> List[Tuple[int, int]]:
    """Uncovered spans [start, end) in ms, at least ``min_s`` long, including before the
    first and after the last sample."""
    out, prev = [], lo_s - 1
    for s in secs + [hi_s]:
        if s - prev - 1 >= min_s:
            out.append(((prev + 1) * 1000, s * 1000))
        prev = s
    return out


def newest_ts(rows: List[list], now_ms: int, shift: int = 0) -> Optional[int]:
    """Newest event time not after ``now_ms`` (rows from the future can only be clock skew)."""
    return max((r[0] + shift for r in rows if r[0] + shift <= now_ms), default=None)


def freshness(newest: Optional[int], now_ms: int) -> Optional[float]:
    return None if newest is None else round((now_ms - newest) / 1000, 1)


def book_cov(rows: List[list], win: Optional[Tuple[int, int]], now_ms: int, evidence: bool) -> dict:
    out = {"seconds": 0, "expected": 0, "pct": None, "holes": [], "age_s": freshness(newest_ts(rows, now_ms), now_ms)}
    if win is None:
        return out
    lo_s, hi_s = win[0] // 1000, win[1] // 1000
    secs = sorted({r[0] // 1000 for r in rows if lo_s <= r[0] // 1000 < hi_s})
    out.update(seconds=len(secs), expected=hi_s - lo_s, pct=pct(len(secs), hi_s - lo_s, evidence))
    if evidence:
        out["holes"] = holes(secs, lo_s, hi_s)
    return out


def kline_cov(rows: List[list], win: Optional[Tuple[int, int]], now_ms: int, evidence: bool,
              rest: Set[int] = frozenset(), gap: Set[int] = frozenset()) -> dict:
    """``minutes``/``pct``: candles the WebSocket delivered; REST candles only in ``rest_minutes``/``complete_*``."""
    ws = [r for r in rows if r[0] // MIN_MS not in rest]
    out = {"minutes": 0, "expected": 0, "pct": None, "rest_minutes": 0, "complete_minutes": 0, "complete_pct": None,
           "lost_minutes": 0, "age_s": freshness(newest_ts(ws, now_ms, MIN_MS), now_ms)}  # age of the newest socket close
    if win is None:
        return out
    first = win[0] // MIN_MS
    expected = (win[1] - win[0]) // MIN_MS  # candles that had closed by the end of the window
    have = {r[0] // MIN_MS for r in rows if first <= r[0] // MIN_MS < first + expected}
    mins = have - rest
    out.update(minutes=len(mins), expected=expected, pct=pct(len(mins), expected, evidence),
               rest_minutes=len(have) - len(mins), complete_minutes=len(have),
               complete_pct=pct(len(have), expected, evidence or bool(have)),
               lost_minutes=len({m for m in gap if first <= m < first + expected} - have))
    return out


def trade_ids(rows: List[list], now_ms: int) -> dict:
    ids = sorted({int(r[1]) for r in rows})
    out = {"rows": len(ids), "id_min": None, "id_max": None, "id_continuity_pct": None,
           "age_s": freshness(newest_ts(rows, now_ms), now_ms)}
    if ids:
        out.update(id_min=ids[0], id_max=ids[-1], id_continuity_pct=100.0 * len(ids) / (ids[-1] - ids[0] + 1))
    return out


def meta_events(rows: List[list]) -> dict:
    ev = {k: 0 for k in EVENTS}
    ev["lost_ids"], ev["gaps"] = 0, []
    for r in rows:
        if len(r) > 1 and r[1] in ev:
            ev[r[1]] += 1
        if len(r) > 3 and r[1] == "trade_gap":
            ev["lost_ids"] += r[3] - r[2] + 1
            ev["gaps"].append([r[2], r[3], r[4] if len(r) > 4 else ""])
    return ev


def coverage(root: str, market: str, symbol: str, day: str, now_ms: Optional[int] = None, late_days: int = 1) -> dict:
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    win = window(day, now_ms)
    rows = {s: list(event_rows(root, market, symbol, s, day, late_days)) for s in ("book", "kline", "trades", "meta")}
    rest, gap = kline_spans(root, market, symbol, day, late_days)
    ws_klines = [r for r in rows["kline"] if r[0] // MIN_MS not in rest]
    # the collector was running for this symbol at some point that day (REST candles alone do not say so)
    evidence = any((rows["book"], ws_klines, rows["trades"], rows["meta"]))
    return {"day": day, "now": now_ms, "evidence": evidence,
            "window": None if win is None else {"start": win[0], "end": win[1], "seconds": (win[1] - win[0]) // 1000},
            "book": book_cov(rows["book"], win, now_ms, evidence),
            "kline": kline_cov(rows["kline"], win, now_ms, evidence, rest, gap),
            "trades": trade_ids(rows["trades"], now_ms),
            "events": meta_events(rows["meta"])}


# -- printing (Thai, for the operator) -----------------------------------------------------
def fmt_pct(x: Optional[float]) -> str:
    return "N/A" if x is None else f"{x:.2f}%"


def fmt_age(s: Optional[float]) -> str:
    if s is None:
        return "N/A"
    return f"{s:.0f} วิ" if s < 120 else f"{s / 60:.0f} นาที" if s < 7200 else f"{s / 3600:.1f} ชม."


def hhmm(ms: int) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(ms / 1000))


def render(market: str, symbol: str, c: dict) -> str:
    w, b, k, t, e = c["window"], c["book"], c["kline"], c["trades"], c["events"]
    if w is None:
        return f"{c['day']} {market} {symbol}: วันนี้ยังไม่ถึง (N/A)"
    longest = sorted(b["holes"], key=lambda h: h[0] - h[1])[:3]
    hole_txt = ", ".join(f"{hhmm(a)}–{hhmm(z)} ({(z - a) / 60000:.0f} นาที)" for a, z in longest) or "ไม่มี"
    lines = [
        f"{c['day']} {market} {symbol} · ช่วงที่ควรมีข้อมูล {hhmm(w['start'])}–{hhmm(w['end'])} UTC ({w['seconds']:,} วิ)",
        f"  book   วินาทีที่มีข้อมูล {fmt_pct(b['pct'])} ({b['seconds']:,}/{b['expected']:,}) · ล่าสุด {fmt_age(b['age_s'])} ที่แล้ว"
        f" · ช่วงหาย ≥{HOLE_S} วิ {len(b['holes'])} ช่วง, ยาวสุด: {hole_txt}",
        f"  kline  แท่ง 1 นาทีที่ปิดแล้วจาก WebSocket {fmt_pct(k['pct'])} ({k['minutes']:,}/{k['expected']:,})"
        f" · ดึงจาก REST {k['rest_minutes']:,} แท่ง (รวมทุกแท่ง {fmt_pct(k['complete_pct'])})"
        + (f" · ดึงไม่ได้ {k['lost_minutes']:,} นาที" if k["lost_minutes"] else "")
        + f" · ล่าสุดจาก WebSocket {fmt_age(k['age_s'])} ที่แล้ว",
        f"  trades {t['rows']:,} รายการ · id ต่อเนื่อง {fmt_pct(t['id_continuity_pct'])} ภายในช่วง id ที่เห็น (ไม่ใช่ uptime)"
        f" · ล่าสุด {fmt_age(t['age_s'])} ที่แล้ว",
        f"  events หลุด {e['disconnected']} · ต่อใหม่ {e['connected']} · book resync {e['book_resync']} · backfill ลองใหม่ {e['backfill_retry']}"
        f" · kline ลองใหม่ {e['kline_retry']}"
        f" · id ที่กู้ไม่ได้ {e['lost_ids']:,}" + (f" ({len(e['gaps'])} ช่วง)" if e["gaps"] else ""),
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="coverage of collected data")
    ap.add_argument("--data", default=os.path.join(LAB, "data", "live"))
    ap.add_argument("--day", action="append", help="UTC day YYYY-MM-DD (repeatable)")
    ap.add_argument("--late-days", type=int, default=1, help="days after the event day to search for late (backfilled) rows")
    args = ap.parse_args(argv)
    now = int(time.time() * 1000)
    days = args.day or [day_name(now - DAY_MS), day_name(now)]
    found = False
    for mdir in sorted(glob.glob(os.path.join(args.data, "*", "*"))):
        if not os.path.isdir(mdir):
            continue
        market, symbol = mdir.split(os.sep)[-2:]
        for day in days:
            c = coverage(args.data, market, symbol, day, now, args.late_days)
            if not c["evidence"]:
                k = c["kline"]
                rest = (f" · มีแต่แท่ง 1 นาทีที่ดึงจาก REST {fmt_pct(k['complete_pct'])} ({k['complete_minutes']:,}/{k['expected']:,})"
                        if k["complete_minutes"] else "")
                print(f"{day} {market} {symbol}: ไม่มีข้อมูลจากการเก็บสด (N/A){rest}")
                continue
            found = True
            print(render(market, symbol, c))
    if not found:
        print("ไม่มีข้อมูลของ", ", ".join(days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
