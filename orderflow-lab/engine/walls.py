"""Order-book walls: detection on one snapshot and persistence across many.

A "wall" is a resting level much larger than its neighbours. One snapshot
cannot say whether it is real; the tracker watches it over time and records
whether it held, grew or shrank, and where price was when it disappeared.

Outcomes describe only what the L2 book showed at removal, never why the size
went away: aggregated levels cannot separate executions from cancellations
and are not matched against traded volume here.

* ``price_reached``  best price on the wall's side touched or crossed its level
* ``removed``        it disappeared while price was still away from it
* ``unknown``        no book to judge by (empty side, or the book lost sync)
"""
from __future__ import annotations

from statistics import median
from typing import Dict, List, Tuple

from feeds.types import BookSnapshot

from .profile import level_of

OUTCOMES = ("price_reached", "removed", "unknown")


def detect_walls(book: BookSnapshot, max_levels: int = 200, min_mult: float = 4.0,
                 min_size: float = 0.0, merge_gap: float = 0.0, max_dist_pct: float = 1.0) -> List[dict]:
    """Levels whose size >= ``min_mult`` x median size of the nearest
    ``max_levels`` on that side, within ``max_dist_pct`` % of mid (far levels on
    thin books are mostly noise). Adjacent walls within ``merge_gap`` price
    units are merged into one (size summed, price of the largest kept)."""
    mid = book.mid or 0.0
    out: List[dict] = []
    for side, levels in (("bid", book.bids[:max_levels]), ("ask", book.asks[:max_levels])):
        if mid and max_dist_pct > 0:
            levels = tuple(lv for lv in levels if abs(lv[0] - mid) / mid * 100.0 <= max_dist_pct)
        sizes = [s for _, s in levels if s > 0]
        if not sizes:
            continue
        med = median(sizes)
        cands = [{"side": side, "price": p, "size": s, "mult": (s / med if med > 0 else 0.0)}
                 for p, s in levels if s >= max(min_size, min_mult * med)]
        merged: List[dict] = []
        for w in cands:
            # tolerance: 100.7 - 100.6 is 0.1000000000000085 in floating point
            if merged and merge_gap > 0 and abs(w["price"] - merged[-1]["edge"]) <= merge_gap * (1 + 1e-6):
                m = merged[-1]
                m["size"] += w["size"]
                m["levels"] += 1
                m["edge"] = w["price"]
                if w["size"] > m["peak"]:
                    m["peak"], m["price"] = w["size"], w["price"]
            else:
                merged.append({**w, "levels": 1, "edge": w["price"], "peak": w["size"]})
        for m in merged:
            m.pop("edge"), m.pop("peak")
            m["dist"] = abs(m["price"] - mid)
            m["dist_pct"] = (m["dist"] / mid * 100.0) if mid else 0.0
            out.append(m)
    out.sort(key=lambda w: -w["size"])
    return out


class WallTracker:
    """Feed snapshots in time order; query ``active()`` and ``events()``."""

    def __init__(self, tick: float, min_mult: float = 4.0, max_levels: int = 200, min_size: float = 0.0,
                 merge_gap: float = 0.0, persist_ms: int = 60_000, miss_limit: int = 2,
                 change_pct: float = 0.25, max_dist_pct: float = 1.0, min_seen: int = 2):
        self.tick = tick
        self.min_mult, self.max_levels, self.min_size, self.merge_gap = min_mult, max_levels, min_size, merge_gap
        self.persist_ms, self.miss_limit, self.change_pct = persist_ms, miss_limit, change_pct
        self.max_dist_pct = max_dist_pct
        self.min_seen = min_seen  # walls seen fewer times are flicker: dropped without an event
        self.walls: Dict[Tuple[str, float], dict] = {}
        self._events: List[dict] = []
        self.last_book: BookSnapshot | None = None

    def update(self, book: BookSnapshot) -> List[dict]:
        seen = set()
        for w in detect_walls(book, self.max_levels, self.min_mult, self.min_size, self.merge_gap, self.max_dist_pct):
            key = (w["side"], level_of(w["price"], self.tick))
            seen.add(key)
            rec = self.walls.get(key)
            if rec is None:
                self.walls[key] = {**w, "first_ts": book.ts, "last_ts": book.ts, "size_first": w["size"],
                                   "size_prev": w["size"], "seen": 1, "misses": 0, "status": "new"}
                continue
            prev = rec["size"]
            change = (w["size"] - prev) / prev if prev > 0 else 0.0
            rec.update(w)
            rec.update({"last_ts": book.ts, "size_prev": prev, "seen": rec["seen"] + 1, "misses": 0,
                        "status": "adding" if change >= self.change_pct else
                        "pulling" if change <= -self.change_pct else "holding"})
        new_events = []
        for key in list(self.walls):
            if key in seen:
                continue
            rec = self.walls[key]
            rec["misses"] += 1
            if rec["misses"] > self.miss_limit:
                del self.walls[key]
                if rec["seen"] < self.min_seen:
                    continue
                new_events.append(self._end(rec, self._outcome(rec, book), book.ts))
        self.last_book = book
        return new_events

    def book_lost(self, ts: int) -> List[dict]:
        """The book feed broke (resync, failed fetch): what happened to the walls
        meanwhile cannot be seen, so every tracked wall ends as ``unknown``."""
        ended = [self._end(rec, "unknown", ts) for rec in self.walls.values() if rec["seen"] >= self.min_seen]
        self.walls.clear()
        return ended

    def _end(self, rec: dict, outcome: str, ts: int) -> dict:
        ev = {**rec, "outcome": outcome, "ended_ts": ts, "age_ms": rec["last_ts"] - rec["first_ts"]}
        self._events.append(ev)
        return ev

    @staticmethod
    def _outcome(rec: dict, book: BookSnapshot) -> str:
        best = book.best_bid if rec["side"] == "bid" else book.best_ask
        if best is None:
            return "unknown"
        reached = best <= rec["price"] if rec["side"] == "bid" else best >= rec["price"]
        return "price_reached" if reached else "removed"

    def active(self, now_ts: int | None = None) -> List[dict]:
        now = now_ts if now_ts is not None else (self.last_book.ts if self.last_book else 0)
        rows = []
        for rec in self.walls.values():
            age = now - rec["first_ts"]
            rows.append({**rec, "age_ms": age, "persistent": age >= self.persist_ms,
                         "growth": (rec["size"] / rec["size_first"] - 1.0) if rec["size_first"] else 0.0})
        rows.sort(key=lambda r: -r["size"])
        return rows

    def events(self, n: int = 20) -> List[dict]:
        return self._events[-n:]
