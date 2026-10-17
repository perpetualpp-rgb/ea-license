"""One-call summaries that stitch the engine modules together.

``summarize_trades`` is the full order-flow picture (needs trades + books).
``summarize_bars`` is the candle-only subset for feeds that have no trade tape,
with every estimate flagged ``approximate``.
"""
from __future__ import annotations

from typing import Iterable, List, Sequence

from feeds.types import Bar, BookSnapshot, Trade

from . import bigtrades, cvd, levels, profile, vwap
from .bars import bars_from_trades
from .walls import WallTracker, detect_walls


def summarize_bars(bars: Sequence[Bar], tick: float, round_step: float, va_pct: float = 0.70,
                   magnet_pct: float = 0.30) -> dict:
    if not bars:
        return {"error": "no bars"}
    prof = profile.profile_from_bars(bars, tick)
    rng = profile.session_range(prof, va_pct)
    magnet = profile.value_area(prof, magnet_pct)
    bands = vwap.vwap_bands(bars)
    last = bars[-1]
    cvd_series = cvd.approx_cvd_from_bars(bars)
    closes = [b.c for b in bars]
    return {
        "mode": "bars", "approximate": True,
        "bars": len(bars), "t_first": bars[0].t, "t_last": last.t,
        "last": last.c, "volume": sum(b.v for b in bars),
        "range": rng, "magnet": {"low": magnet[0], "high": magnet[1], "pct": magnet_pct},
        "nodes": profile.nodes(prof, top=3),
        "vwap": {**bands[-1], "position": vwap.position(last.c, bands[-1]),
                 "shape": vwap.band_shape(bands)} if bands else None,
        "cvd": {"value": cvd_series[-1][1], "approximate": True,
                "divergence": cvd.divergence(closes, [c for _, c in cvd_series])},
        "round_levels": levels.round_levels(rng["low"], rng["high"], round_step),
        "nearest_round": levels.nearest(last.c, round_step),
    }


def summarize_trades(trades: Sequence[Trade], books: Sequence[BookSnapshot], tick: float, round_step: float,
                     interval_ms: int = 60_000, va_pct: float = 0.70, magnet_pct: float = 0.30,
                     big_pct: float = 0.99, big_min: float = 0.0, wall_mult: float = 4.0,
                     wall_depth: int = 200, persist_ms: int = 60_000, wall_max_dist_pct: float = 1.0) -> dict:
    if not trades:
        return {"error": "no trades"}
    bars = bars_from_trades(trades, interval_ms)
    out = summarize_bars(bars, tick, round_step, va_pct, magnet_pct)
    out.update({"mode": "trades", "approximate": False, "trades": len(trades),
                "last": trades[-1].price, "ts_first": trades[0].ts, "ts_last": trades[-1].ts})

    prof = profile.profile_from_trades(trades, tick)
    out["range"] = profile.session_range(prof, va_pct)
    magnet = profile.value_area(prof, magnet_pct)
    out["magnet"] = {"low": magnet[0], "high": magnet[1], "pct": magnet_pct}
    out["nodes"] = profile.nodes(prof, top=3)
    out["profile_buy"] = sum(v["buy"] for v in prof.levels.values())
    out["profile_sell"] = sum(v["sell"] for v in prof.levels.values())

    buckets = cvd.delta_buckets(trades, interval_ms)
    series = cvd.cvd_from_buckets(buckets)
    out["cvd"] = {"value": series[-1][1], "approximate": False,
                  "last_buckets": buckets[-5:],
                  "divergence": cvd.divergence([b.c for b in bars], [c for _, c in series])}

    thr = bigtrades.threshold(trades, big_pct, big_min)
    big = bigtrades.big_trades(trades, thr)
    out["big_trades"] = {"threshold": thr, "pct": big_pct, "count": len(big),
                         "zones": bigtrades.zones(big, tick)[:8],
                         "recent": [{"ts": t.ts, "price": t.price, "size": t.size, "side": t.side}
                                    for t in big[-5:]]}

    tracker = WallTracker(tick, min_mult=wall_mult, max_levels=wall_depth, merge_gap=tick, persist_ms=persist_ms,
                          max_dist_pct=wall_max_dist_pct)
    for b in books:
        tracker.update(b)
    active = tracker.active()
    keep = ("side", "price", "size", "mult", "levels", "dist", "dist_pct", "status", "seen", "age_ms", "persistent", "growth")
    out["walls"] = {
        "snapshots": len(books),
        "bid": [{k: w[k] for k in keep} for w in active if w["side"] == "bid"][:5],
        "ask": [{k: w[k] for k in keep} for w in active if w["side"] == "ask"][:5],
        "events": [{k: e[k] for k in ("side", "price", "size", "outcome", "age_ms", "ended_ts")} for e in tracker.events(10)],
        "book_ts": books[-1].ts if books else None,
        "best_bid": books[-1].best_bid if books else None,
        "best_ask": books[-1].best_ask if books else None,
    }
    return out


def one_liner(label: str, trades_new: List[Trade], book: BookSnapshot | None, wall_mult: float = 4.0) -> str:
    """Compact status line for the collector."""
    last = trades_new[-1].price if trades_new else (book.mid if book else None)
    delta = sum(t.signed_size for t in trades_new)
    parts = [f"{label:<14}", f"last={last}", f"new={len(trades_new)}", f"delta={delta:+.4f}"]
    if book:
        ws = detect_walls(book, min_mult=wall_mult)
        bid = next((w for w in ws if w["side"] == "bid"), None)
        ask = next((w for w in ws if w["side"] == "ask"), None)
        parts.append(f"bidwall={bid['price']}x{bid['size']:.3f}" if bid else "bidwall=-")
        parts.append(f"askwall={ask['price']}x{ask['size']:.3f}" if ask else "askwall=-")
    return "  ".join(parts)
