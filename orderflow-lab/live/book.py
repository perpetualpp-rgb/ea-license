"""Local order book kept in sync from a Binance REST snapshot plus the diff stream.

Binance documents two slightly different procedures:

* futures (``@depth`` on fstream): after the snapshot, drop events with
  ``u < lastUpdateId``; the first applied event must have
  ``U <= lastUpdateId <= u``; every later event's ``pu`` must equal the
  previous event's ``u``.
* spot (``@depth`` on stream/data-stream): drop events with
  ``u <= lastUpdateId``; the first applied event must have
  ``U <= lastUpdateId + 1 <= u``; later events must not skip ids
  (``U == previous u + 1``).

Any break means the book is no longer trustworthy: it is cleared and the
caller fetches a new snapshot. Events that arrive before the snapshot are
buffered so nothing is lost while the snapshot request is in flight.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

MAX_BUFFER = 5000  # diff events held while waiting for a snapshot


class LocalBook:
    def __init__(self, market: str):
        if market not in ("spot", "futures"):
            raise ValueError(market)
        self.market = market
        self.bids: Dict[str, float] = {}
        self.asks: Dict[str, float] = {}
        self.last_id: Optional[int] = None  # id of the last applied update
        self.synced = False
        self.fresh = False
        self.buffer: List[dict] = []
        self.resyncs = 0

    # -- lifecycle -----------------------------------------------------------
    def reset(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.last_id = None
        self.synced = False
        self.fresh = False
        self.buffer = []

    def needs_snapshot(self) -> bool:
        return not self.synced and bool(self.buffer)

    def on_event(self, ev: dict) -> bool:
        """Feed one diff event. Returns False when the book broke and needs a new snapshot."""
        if not self.synced:
            self.buffer.append(ev)
            if len(self.buffer) > MAX_BUFFER:
                self.buffer = self.buffer[-MAX_BUFFER:]
            return True
        if not self._feed(ev):
            self.buffer.append(ev)
            return False
        return True

    def load_snapshot(self, snap: dict) -> bool:
        """Apply a REST depth snapshot and replay buffered events.

        Returns False when the snapshot is older than the buffered stream or the
        buffered events do not bridge it; fetch another snapshot then.
        """
        sid = int(snap["lastUpdateId"])
        pending = self.buffer
        if pending and sid < int(pending[0]["U"]) - 1:
            return False  # snapshot predates the first buffered event; keep buffering
        self.bids = {p: float(q) for p, q in snap["bids"] if float(q) > 0}
        self.asks = {p: float(q) for p, q in snap["asks"] if float(q) > 0}
        self.last_id = sid
        self.synced = True
        self.fresh = True  # the next applied event must bridge the snapshot id
        self.buffer = []
        for i, ev in enumerate(pending):
            if not self._feed(ev):
                self.buffer = pending[i:]
                return False
        return True

    # -- internals -----------------------------------------------------------
    def _feed(self, ev: dict) -> bool:
        U, u, last = int(ev["U"]), int(ev["u"]), self.last_id
        if self.fresh:
            if self.market == "futures":
                if u < last:
                    return True  # older than the snapshot
                ok = U <= last <= u
            else:
                if u <= last:
                    return True
                ok = U <= last + 1 <= u
            if not ok:
                self._break()
                return False
            self.fresh = False
        elif self.market == "futures":
            if int(ev.get("pu", -1)) != last:
                self._break()
                return False
        else:
            if u <= last:
                return True  # stale duplicate
            if U > last + 1:
                self._break()
                return False
        self._apply(ev)
        return True

    def _apply(self, ev: dict) -> None:
        for side, book in (("b", self.bids), ("a", self.asks)):
            for p, q in ev.get(side, ()):
                q = float(q)
                if q == 0:
                    book.pop(p, None)
                else:
                    book[p] = q
        self.last_id = int(ev["u"])

    def _break(self) -> None:
        self.resyncs += 1
        self.bids.clear()
        self.asks.clear()
        self.synced = False
        self.fresh = False
        self.last_id = None

    # -- views ---------------------------------------------------------------
    def best(self) -> Tuple[Optional[float], Optional[float]]:
        bb = max((float(p) for p in self.bids), default=None)
        ba = min((float(p) for p in self.asks), default=None)
        return bb, ba

    def buckets(self, bucket: float, window_pct: float):
        """Aggregate the book into price buckets within ``window_pct`` of mid.

        Bids are floored and asks ceiled to the bucket grid (same convention as
        the dashboard), sorted best-first. Returns (mid, bids, asks) or None.
        """
        bb, ba = self.best()
        if bb is None or ba is None:
            return None
        mid = (bb + ba) / 2
        lo, hi = mid * (1 - window_pct / 100), mid * (1 + window_pct / 100)
        nd = max(0, -int(math.floor(math.log10(bucket)))) if bucket < 1 else 0
        bids: Dict[float, float] = {}
        asks: Dict[float, float] = {}
        for p, q in self.bids.items():
            x = float(p)
            if x >= lo:
                k = round(math.floor(x / bucket + 1e-9) * bucket, nd + 2)
                bids[k] = bids.get(k, 0.0) + q
        for p, q in self.asks.items():
            x = float(p)
            if x <= hi:
                k = round(math.ceil(x / bucket - 1e-9) * bucket, nd + 2)
                asks[k] = asks.get(k, 0.0) + q
        rb = [[k, round(v, 8)] for k, v in sorted(bids.items(), reverse=True)]
        ra = [[k, round(v, 8)] for k, v in sorted(asks.items())]
        return mid, rb, ra
