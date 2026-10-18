"""Market state for the quote/details/observations endpoints, computed from our database.

Numbers come from the same Python engine the desk and the web dashboard use
(``orderflow-lab/engine``), with the desk's parameters:

* walls: ``engine.walls.WallTracker`` fed with stored book bucket summaries, one per 10 s
  (the desk polls the depth every 10 s) over the 30 minutes before ``to``;
* volume profile: ``engine.profile.profile_from_trades`` (or ``profile_from_bars`` on 1m
  klines, an approximation) + ``poc`` / ``value_area``;
* big trades: the nearest-rank percentile of ``engine.bigtrades.threshold``. It is computed
  in PostgreSQL with ``percentile_disc``, which uses the identical rank formula
  (ceil(p * n) on float8), so millions of trade sizes are not shipped to Python; a test
  checks both give the same threshold.

Every query is bounded by ``to``, which the router has already clamped to the member's
entitlement cut-off, so a delayed member never receives a row newer than the cut-off.
``None`` means "cannot be computed from the stored data", never zero.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal
from pathlib import Path
from statistics import median
from typing import Dict, List, Optional, Tuple

import psycopg

from .common import dstr, iso
from .routers.market import gaps_as_of

# orderflow-lab/ holds the engine (server/app/orderflow.py -> parents[2]); added the same way
# live/collector.py does. A deployment must keep that layout: copy engine/ and feeds/ next to
# server/ (see server/Dockerfile).
LAB = str(Path(__file__).resolve().parents[2])
if LAB not in sys.path:
    sys.path.insert(0, LAB)

from engine import profile as eng_profile  # noqa: E402
from engine.walls import WallTracker  # noqa: E402
from feeds.types import Bar, BookSnapshot, Trade, sort_book  # noqa: E402

UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
ORIGIN = datetime(2000, 1, 3, tzinfo=UTC)
GAP_BAD = ("open", "partial", "unrecoverable")

# book / walls: the desk's settings (desk/orderflow-desk.html startBinance)
BOOK_LOOKBACK = timedelta(minutes=30)
BOOK_SAMPLE_S = 10
BOOK_STALE = timedelta(seconds=60)
WALL_PARAMS = dict(min_mult=4.0, max_levels=200, merge_gap=0.0, persist_ms=60_000, miss_limit=2,
                   change_pct=0.25, max_dist_pct=1.0, min_seen=2)
MAX_WALLS, MAX_EVENTS = 10, 10

# big trades
BIG_PCT = 0.99
BIG_LOOKBACK = timedelta(hours=4)
BIG_MIN_TRADES = 500
BIG_ITEMS = 50

# profile
VA_PCT = 0.70
PROFILE_DIVISOR = Decimal(5000)          # bin ~ price / 5000 rounded down to 1-2-5: BTC 80k -> 10, 4000 -> 0.5
PROFILE_TRADES_MAX_RANGE = timedelta(days=1)
PROFILE_MAX_GROUPS = 200_000

# open interest
OI_MAX_POINTS = 360

# quality: expected interval between rows (s); stale = newest row older than 3 x this before `to`.
# trades and klines use fixed values; book, mark and oi use their stored spacing (spacing_s), with
# the collector defaults (live/config.json book_every_s, mark_every_s, oi_every_s) as fallback.
EXPECTED_S = {"trades": 60, "kline": 60}
DEFAULT_SPACING_S = {"book": 1, "mark": 5, "oi": 60}
STALE_FACTOR = 3
QUALITY_GAP_WINDOW = timedelta(minutes=5)
QUALITY_STREAMS = (("trades", "trades"), ("book", "book"), ("kline", "kline"), ("oi", "oi"), ("mark", "mark"),
                   ("liquidations", "liq"))
SPOT_NOTE = "ตลาด spot ไม่มี open interest, funding rate, mark price และ liquidation (มีเฉพาะสัญญา futures)"

# observations
OBS_CVD_S, OBS_OI_S, OBS_BIG_S = 300, 300, 1800
OBS_FUNDING_MAX_AGE = timedelta(minutes=15)
OBS_ORDER = ("cvd", "oi", "wall", "bigtrade", "funding")


# -- small helpers --------------------------------------------------------------------------
def to_ms(t: datetime) -> int:
    return int((t - EPOCH) / timedelta(milliseconds=1))


def from_ms(v: int) -> datetime:
    return EPOCH + timedelta(milliseconds=int(v))


def fdec(x: float) -> Decimal:
    """Shortest decimal that round-trips the float (what the stored text was)."""
    return Decimal(repr(float(x)))


def fstr(x: Optional[float]) -> Optional[str]:
    return None if x is None else dstr(fdec(x))


def pct2(new: Optional[Decimal], old: Optional[Decimal]) -> Optional[str]:
    """(new / old - 1) x 100 with two decimals (half-even); None when either is missing."""
    if new is None or old is None or old == 0:
        return None
    s = format(((Decimal(new) / Decimal(old) - 1) * 100).quantize(Decimal("0.01"), ROUND_HALF_EVEN), "f")
    return "0.00" if s == "-0.00" else s


def rate_pct(rate: Decimal) -> str:
    """Funding fraction as percent, exact, at least four decimals (0.0001 -> "0.0100")."""
    v = Decimal(rate) * 100
    s = dstr(v)
    if len(s.partition(".")[2]) < 4:
        s = format(v.quantize(Decimal("0.0001")), "f")
    return "0.0000" if s == "-0.0000" else s


def _dec(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _score(x: float) -> float:
    return max(0.0, x)


def bad_gaps(gaps: List[dict]) -> List[dict]:
    return [g for g in gaps if g["status"] in GAP_BAD]


def _ts(s: Optional[str]) -> Optional[datetime]:
    return None if s is None else datetime.fromisoformat(s.replace("Z", "+00:00"))


def trade_gaps(conn: psycopg.Connection, iid: int, frm: datetime, to: datetime, cutoff: datetime) -> List[dict]:
    return bad_gaps(gaps_as_of(conn, iid, frm, to, cutoff, ["trades"]))


# -- prices ---------------------------------------------------------------------------------
def last_trade(conn: psycopg.Connection, iid: int, to: datetime, frm: Optional[datetime] = None) -> Optional[dict]:
    return conn.execute(
        "SELECT price, event_ts FROM trades WHERE instrument_id = %s AND event_ts < %s"
        " AND (%s::timestamptz IS NULL OR event_ts >= %s::timestamptz) ORDER BY event_ts DESC, agg_trade_id DESC LIMIT 1",
        [iid, to, frm, frm]).fetchone()


def last_kline(conn: psycopg.Connection, iid: int, to: datetime, frm: Optional[datetime] = None) -> Optional[dict]:
    """Newest closed 1m kline whose close time is <= ``to`` (and >= ``frm``)."""
    return conn.execute(
        "SELECT close, open_time + interval '60 seconds' AS close_at FROM klines"
        " WHERE instrument_id = %s AND interval_s = 60 AND open_time <= %s - interval '60 seconds'"
        " AND (%s::timestamptz IS NULL OR open_time >= %s::timestamptz - interval '60 seconds')"
        " ORDER BY open_time DESC LIMIT 1", [iid, to, frm, frm]).fetchone()


def last_mark(conn: psycopg.Connection, iid: int, to: datetime, frm: Optional[datetime] = None) -> Optional[dict]:
    return conn.execute(
        "SELECT event_ts, mark_price, index_price, funding_rate, next_funding_ts FROM mark_prices"
        " WHERE instrument_id = %s AND event_ts < %s AND (%s::timestamptz IS NULL OR event_ts >= %s::timestamptz)"
        " ORDER BY event_ts DESC LIMIT 1", [iid, to, frm, frm]).fetchone()


def first_last_price(conn: psycopg.Connection, iid: int, frm: datetime, to: datetime) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    first = conn.execute("SELECT price FROM trades WHERE instrument_id = %s AND event_ts >= %s AND event_ts < %s"
                         " ORDER BY event_ts, agg_trade_id LIMIT 1", [iid, frm, to]).fetchone()
    last = last_trade(conn, iid, to, frm)
    return (first["price"] if first else None), (last["price"] if last else None)


# -- quote ------------------------------------------------------------------------------------
def quote(conn: psycopg.Connection, inst: dict, cutoff: datetime) -> dict:
    iid = inst["instrument_id"]
    lt = last_trade(conn, iid, cutoff)
    if lt:
        price, at, source = lt["price"], lt["event_ts"], "trade"
    else:
        k = last_kline(conn, iid, cutoff)
        price, at, source = (k["close"], k["close_at"], "kline") if k else (None, None, None)
    out = {"last_trade": {"price": dstr(price), "at": iso(at), "source": source} if price is not None else None,
           "mark": None, "change_24h": None, "spark": []}
    if inst["market_type"] != "spot":
        m = last_mark(conn, iid, cutoff)
        if m:
            out["mark"] = {"price": dstr(m["mark_price"]), "index": dstr(m["index_price"]), "at": iso(m["event_ts"])}
    if price is None:
        return out
    target = at - timedelta(hours=24)
    ref = conn.execute(
        "SELECT close, open_time + interval '60 seconds' AS close_at FROM klines"
        " WHERE instrument_id = %(id)s AND interval_s = 60"
        " AND open_time BETWEEN %(t)s - interval '6 minutes' AND %(t)s + interval '4 minutes'"
        " ORDER BY abs(extract(epoch FROM open_time + interval '60 seconds' - %(t)s)), open_time LIMIT 1",
        {"id": iid, "t": target}).fetchone()
    if ref and ref["close"]:
        out["change_24h"] = {"pct": pct2(price, ref["close"]), "ref_price": dstr(ref["close"]),
                             "ref_at": iso(ref["close_at"])}
    rows = conn.execute(
        """
        SELECT k.close_at, k.close
        FROM generate_series(48, 0, -1) AS g(i)
        CROSS JOIN LATERAL (
          SELECT close, open_time + interval '60 seconds' AS close_at FROM klines
          WHERE instrument_id = %(id)s AND interval_s = 60
            AND open_time <= %(at)s - make_interval(mins => 30 * g.i) - interval '60 seconds'
            AND open_time > %(at)s - make_interval(mins => 30 * g.i) - interval '31 minutes'
          ORDER BY open_time DESC LIMIT 1) k
        ORDER BY g.i DESC
        """, {"id": iid, "at": at}).fetchall()
    out["spark"] = [{"t": iso(r["close_at"]), "close": dstr(r["close"])} for r in rows]
    return out


# -- book / walls -----------------------------------------------------------------------------
def _levels(raw) -> List[Tuple[Decimal, Decimal]]:
    out = []
    for lv in raw or []:
        try:
            out.append((_dec(lv[0]), _dec(lv[1])))
        except (IndexError, TypeError, ArithmeticError, ValueError):
            continue
    return out


def infer_bucket(rows: List[dict], tick: Optional[Decimal]) -> Tuple[Optional[Decimal], str]:
    """Bucket width of the stored book summaries: the value ingest recorded from the collector
    config, else the smallest step between adjacent price levels on one side, else the tick."""
    newest = rows[-1]
    if newest.get("bucket_size"):
        return Decimal(newest["bucket_size"]), "ตามที่ตั้งไว้ใน collector"
    steps = []
    for r in rows[-5:]:
        for side in ("bids", "asks"):
            prices = sorted({p for p, _ in _levels(r[side])})
            steps += [b - a for a, b in zip(prices, prices[1:]) if b > a]
    if steps:
        return min(steps), "อนุมานจากระยะห่างของระดับราคาใน book ล่าสุด"
    if tick:
        return Decimal(tick), "ใช้ tick ของสัญญา เพราะ book มีระดับเดียว อนุมานไม่ได้"
    return None, "ระบุขนาดช่องราคาไม่ได้ (book มีระดับเดียวและไม่รู้ tick ของสัญญา) จึงไม่คำนวณกำแพง"


def _snapshot(inst: dict, r: dict) -> BookSnapshot:
    bids, asks = sort_book(((float(p), float(q)) for p, q in _levels(r["bids"])),
                           ((float(p), float(q)) for p, q in _levels(r["asks"])))
    return BookSnapshot(to_ms(r["captured_ts"]), inst["venue"], inst["symbol"], bids, asks)


def _wall_out(w: dict) -> dict:
    return {"side": w["side"], "price": fstr(w["price"]), "size": fstr(w["size"]),
            "mult": format(fdec(w["mult"]).quantize(Decimal("0.1"), ROUND_HALF_EVEN), "f"),
            "age_s": int(w["age_ms"] // 1000), "status": w["status"]}


def book_state(conn: psycopg.Connection, inst: dict, to: datetime, cutoff: datetime) -> Optional[dict]:
    iid = inst["instrument_id"]
    frm = to - BOOK_LOOKBACK
    rows = conn.execute(
        """
        WITH pick AS (
          SELECT DISTINCT ON (date_bin(make_interval(secs => %(step)s), captured_ts, %(origin)s)) captured_ts
          FROM book_snapshots
          WHERE instrument_id = %(id)s AND kind = 'bucket_summary' AND captured_ts >= %(frm)s AND captured_ts < %(to)s
          ORDER BY date_bin(make_interval(secs => %(step)s), captured_ts, %(origin)s), captured_ts DESC)
        SELECT b.captured_ts, b.bids, b.asks, b.bucket_size, b.synced
        FROM book_snapshots b JOIN pick USING (captured_ts)
        WHERE b.instrument_id = %(id)s AND b.kind = 'bucket_summary'
        ORDER BY b.captured_ts
        """, {"id": iid, "frm": frm, "to": to, "step": BOOK_SAMPLE_S, "origin": ORIGIN}).fetchall()
    if not rows:
        old = conn.execute("SELECT captured_ts, synced FROM book_snapshots WHERE instrument_id = %s"
                           " AND kind = 'bucket_summary' AND captured_ts < %s ORDER BY captured_ts DESC LIMIT 1",
                           [iid, to]).fetchone()
        if old is None:
            return None
        return {"at": iso(old["captured_ts"]), "synced": bool(old["synced"]), "walls": [], "events": [], "top": None,
                "method": "ไม่มี order book ใน 30 นาทีล่าสุดของช่วงนี้ จึงไม่คำนวณกำแพง"}
    newest = rows[-1]
    at = newest["captured_ts"]
    broken = [g for g in bad_gaps(gaps_as_of(conn, iid, at, to, cutoff, ["book"]))
              if g["gap_end"] is None or _ts(g["gap_end"]) > at]
    synced = bool(newest["synced"]) and not broken
    bucket, bucket_src = infer_bucket(rows, inst.get("tick_size"))
    # plain Thai for members; every parameter that decides what is shown is stated
    method_bits = [
        f"ดู order book ทุก {BOOK_SAMPLE_S} วินาที ย้อนหลัง 30 นาที ({len(rows)} ครั้ง)",
        f"กำแพง = ช่องราคาที่มีคำสั่งรอ ≥ {WALL_PARAMS['min_mult']:g} เท่าของค่ากลาง {WALL_PARAMS['max_levels']} ช่องใกล้ราคา"
        f" ภายใน ±{WALL_PARAMS['max_dist_pct']:g}% · หายไปเกิน {WALL_PARAMS['miss_limit']} ครั้ง = จบ"
        f" · ขนาดเปลี่ยน ≥ {WALL_PARAMS['change_pct'] * 100:g}% = เติม/ลดลง",
        "อายุนับได้ไม่เกิน 30 นาที (กำแพงที่อยู่มานานกว่านั้นจะแสดงไม่เกิน 30 นาที)",
        "ตอนกำแพงหายไป บอกได้แค่ว่าราคาไปถึงหรือไม่ ไม่รู้ว่าถูกจับคู่หรือถูกยกเลิก"
        f" · ถ้า book ขาดเกิน {2 * BOOK_SAMPLE_S} วินาทีหรือใช้ไม่ได้ = ไม่ทราบ",
    ]
    if bucket is None:
        return {"at": iso(at), "synced": synced, "walls": [], "events": [], "top": None, "method": bucket_src}
    method_bits.insert(1, f"รวมราคาเป็นช่องละ {dstr(bucket)} {inst['quote_asset']} ({bucket_src})")
    tracker = WallTracker(float(bucket), **WALL_PARAMS)
    gap_ms = 2 * BOOK_SAMPLE_S * 1000
    prev_ms: Optional[int] = None
    for r in rows:
        snap = _snapshot(inst, r)
        if prev_ms is not None and snap.ts - prev_ms > gap_ms:
            tracker.book_lost(prev_ms + gap_ms)          # the desk declares walls lost after two missed polls
        if not r["synced"]:
            tracker.book_lost(snap.ts)
        else:
            tracker.update(snap)
        prev_ms = snap.ts
    stale = to - at > BOOK_STALE
    walls: List[dict] = []
    if stale or not synced:
        lost = at + BOOK_STALE if stale else max(at, min(_ts(g["gap_start"]) for g in broken) if broken else at)
        tracker.book_lost(to_ms(min(lost, to)))
        method_bits.append("order book ล่าสุดเก่ากว่า 60 วินาที จึงไม่แสดงกำแพงตอนนี้" if stale
                           else "order book ใช้ไม่ได้ตอนนี้ (กำลังเริ่มใหม่หรือการเชื่อมต่อหลุด) จึงไม่แสดงกำแพง")
    else:
        walls = tracker.active(snap.ts)[:MAX_WALLS]
    events = [{"side": e["side"], "price": fstr(e["price"]), "size": fstr(e["size"]), "outcome": e["outcome"],
               "at": iso(from_ms(e["ended_ts"]))} for e in reversed(tracker.events(MAX_EVENTS))]
    out_walls = [_wall_out(w) for w in walls]
    return {"at": iso(at), "synced": synced, "walls": out_walls, "events": events,
            "top": out_walls[0] if out_walls else None, "method": " · ".join(method_bits)}


# -- big trades --------------------------------------------------------------------------------
def big_threshold(conn: psycopg.Connection, iid: int, frm: datetime, to: datetime) -> Tuple[Optional[Decimal], str, int, int]:
    """(threshold, basis 'pre_window'|'window'|'none', trades used, trades in the pre-window)."""
    sql = ("SELECT count(*) AS n, percentile_disc(%s::float8) WITHIN GROUP (ORDER BY qty) AS thr FROM trades"
           " WHERE instrument_id = %s AND event_ts >= %s AND event_ts < %s")
    pre = conn.execute(sql, [BIG_PCT, iid, frm - BIG_LOOKBACK, frm]).fetchone()
    if pre["n"] >= BIG_MIN_TRADES:
        return pre["thr"], "pre_window", pre["n"], pre["n"]
    win = conn.execute(sql, [BIG_PCT, iid, frm, to]).fetchone()
    if win["n"]:
        return win["thr"], "window", win["n"], pre["n"]
    return None, "none", 0, pre["n"]


def big_trades(conn: psycopg.Connection, inst: dict, frm: datetime, to: datetime) -> dict:
    iid = inst["instrument_id"]
    thr, basis, n, n_pre = big_threshold(conn, iid, frm, to)
    # nearest-rank percentile, the same as engine.bigtrades.threshold (percentile_disc in SQL)
    rule = f"ไม้ใหญ่ = เทรดที่ขนาด ≥ เปอร์เซ็นไทล์ที่ {BIG_PCT * 100:g} ของขนาดเทรด"
    if basis == "none":
        return {"threshold": None, "items": [],
                "method": rule + " · ไม่มีเทรดทั้งใน 4 ชั่วโมงก่อนช่วงที่แสดงและในช่วงที่แสดง จึงคำนวณเกณฑ์ไม่ได้"}
    if basis == "pre_window":
        src = f"เกณฑ์คิดจากเทรด {n:,} รายการใน 4 ชั่วโมงก่อนช่วงที่แสดง (ไม่ใช้ข้อมูลในช่วงที่แสดง)"
    else:
        src = (f"4 ชั่วโมงก่อนช่วงที่แสดงมีเทรดเพียง {n_pre:,} รายการ (ต้องการ ≥ {BIG_MIN_TRADES:,}) จึงคิดเกณฑ์จากเทรด"
               f" {n:,} รายการในช่วงที่แสดงเอง: เกณฑ์นี้รู้ข้อมูลของทั้งช่วงล่วงหน้า (look-ahead)")
    rows = conn.execute(
        "SELECT event_ts, taker_side, price, qty FROM trades WHERE instrument_id = %s AND event_ts >= %s"
        " AND event_ts < %s AND qty >= %s AND taker_side IN ('buy', 'sell')"
        " ORDER BY event_ts DESC, agg_trade_id DESC LIMIT %s", [iid, frm, to, thr, BIG_ITEMS]).fetchall()
    unknown = conn.execute("SELECT count(*) AS n FROM trades WHERE instrument_id = %s AND event_ts >= %s"
                           " AND event_ts < %s AND qty >= %s AND taker_side = 'unknown'",
                           [iid, frm, to, thr]).fetchone()["n"]
    items = [{"at": iso(r["event_ts"]), "side": r["taker_side"], "price": dstr(r["price"]), "size": dstr(r["qty"])}
             for r in rows]
    return {"threshold": dstr(thr), "items": items,
            "method": (f"{rule} · {src} · แสดงล่าสุดไม่เกิน {BIG_ITEMS} ไม้ · ฝั่ง = ฝั่งที่กดซื้อ/ขายทันที (taker)"
                       f" · ไม่รวม {unknown:,} ไม้ที่ไม่ทราบฝั่ง taker")}


# -- volume profile -----------------------------------------------------------------------------
def profile_bin(ref: Decimal, tick: Optional[Decimal]) -> Decimal:
    """price / 5000 rounded down to the 1-2-5 series (BTC ~80000 -> 10, gold ~4000 -> 0.5),
    then at least one tick and a whole number of ticks when the tick size is known."""
    raw = Decimal(ref) / PROFILE_DIVISOR
    base = Decimal(1).scaleb(raw.adjusted())
    step = next(base * m for m in (5, 2, 1) if base * m <= raw)
    if tick:
        tick = Decimal(tick)
        step = tick if step < tick else (step / tick).to_integral_value(ROUND_CEILING) * tick
    return step


def _q(x: Optional[float], step: Decimal) -> Optional[str]:
    if x is None:
        return None
    exp = min(0, step.normalize().as_tuple().exponent)
    return dstr(fdec(x).quantize(Decimal(1).scaleb(exp), ROUND_HALF_EVEN))


def profile_state(conn: psycopg.Connection, inst: dict, frm: datetime, to: datetime, cutoff: datetime) -> Optional[dict]:
    iid = inst["instrument_id"]
    lt = last_trade(conn, iid, to, frm)
    lk = last_kline(conn, iid, to, frm)
    ref = lt["price"] if lt else lk["close"] if lk else None
    if ref is None or ref <= 0:
        return None
    step = profile_bin(ref, inst.get("tick_size"))
    # engine.profile.poc / value_area; bin from price / 5000 on the 1-2-5 series
    rule = (f"ช่องราคาละ {dstr(step)} (ราคา ÷ 5,000 ปัดลงเป็น 1-2-5" + (" เป็นจำนวนเต็มเท่าของ tick" if inst.get("tick_size") else "")
            + f") · POC = ช่องที่ซื้อขายมากที่สุด · value area = {VA_PCT * 100:g}% ของปริมาณรอบ POC")
    reason = None
    if to - frm > PROFILE_TRADES_MAX_RANGE:
        reason = "ช่วงยาวกว่า 1 วัน"
    elif lt is None:
        reason = "ไม่มีเทรดในช่วง"
    elif trade_gaps(conn, iid, frm, to, cutoff) and lk is not None:
        reason = "มีช่วงเทรดขาดที่ยังไม่ได้คืนในช่วงนี้"
    prof = None
    if reason is None:
        groups = conn.execute(
            "SELECT price, taker_side, sum(qty) AS qty FROM trades WHERE instrument_id = %s AND event_ts >= %s"
            " AND event_ts < %s GROUP BY price, taker_side LIMIT %s", [iid, frm, to, PROFILE_MAX_GROUPS + 1]).fetchall()
        if len(groups) > PROFILE_MAX_GROUPS:
            reason = "ระดับราคาในเทรดมากเกินกำหนด"
        else:
            # one Trade per (price, side) carrying the summed size gives the same levels as feeding every print
            trades = [Trade(0, float(g["price"]), float(g["qty"]), g["taker_side"], inst["venue"], inst["symbol"], "")
                      for g in groups]
            prof = eng_profile.profile_from_trades(trades, float(step))
            note = "จากเทรดทุกรายการในช่วงที่แสดง"  # engine.profile.profile_from_trades
            if trade_gaps(conn, iid, frm, to, cutoff):
                note += " (มีช่วงเทรดขาดและไม่มีแท่ง 1 นาทีให้แทน ค่าจึงอาจไม่ครบ)"
    if prof is None:
        if lk is None:
            return None
        bars = [Bar(int(r["t"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                    float(r["volume"])) for r in conn.execute(
            "SELECT extract(epoch FROM open_time)::bigint AS t, open, high, low, close, volume FROM klines"
            " WHERE instrument_id = %s AND interval_s = 60 AND open_time >= %s"
            " AND open_time <= %s - interval '60 seconds' ORDER BY open_time", [iid, frm, to]).fetchall()]
        if not bars:
            return None
        prof = eng_profile.profile_from_bars(bars, float(step))
        note = f"ค่าประมาณจากแท่ง 1 นาที (กระจายปริมาณแต่ละแท่งเท่ากันตลอด high–low) เพราะ{reason}"  # profile_from_bars
    poc = eng_profile.poc(prof)
    va = eng_profile.value_area(prof, VA_PCT)
    return {"bin_size": dstr(step), "poc": _q(poc, step), "va_low": _q(va[0], step) if va else None,
            "va_high": _q(va[1], step) if va else None, "method": f"{note} · {rule}"}


# -- open interest / funding ----------------------------------------------------------------------
_SPACING_SQL = {
    "oi": "SELECT event_ts AS t FROM open_interest WHERE instrument_id = %s AND event_ts < %s ORDER BY event_ts DESC LIMIT 11",
    "mark": "SELECT event_ts AS t FROM mark_prices WHERE instrument_id = %s AND event_ts < %s ORDER BY event_ts DESC LIMIT 11",
    "book": "SELECT captured_ts AS t FROM book_snapshots WHERE instrument_id = %s AND kind = 'bucket_summary'"
            " AND captured_ts < %s ORDER BY captured_ts DESC LIMIT 11",
}


def spacing_s(conn: psycopg.Connection, stream: str, iid: int, to: datetime) -> float:
    """Usual seconds between rows of a polled/sampled stream: the median spacing of the newest
    eleven rows before ``to`` (the collector's oi_every_s / mark_every_s / book_every_s may be
    changed), else the collector default."""
    rows = conn.execute(_SPACING_SQL[stream], [iid, to]).fetchall()
    diffs = [d for d in ((a["t"] - b["t"]).total_seconds() for a, b in zip(rows, rows[1:])) if d > 0]
    return max(1.0, median(diffs)) if diffs else float(DEFAULT_SPACING_S[stream])


def oi_poll_s(conn: psycopg.Connection, iid: int, to: datetime) -> float:
    return spacing_s(conn, "oi", iid, to)


def oi_at_distance(conn: psycopg.Connection, iid: int, base: dict, seconds: int, poll: float) -> Optional[dict]:
    """The OI row nearest ``base.at - seconds`` within +- one polling interval, else None."""
    target = base["event_ts"] - timedelta(seconds=seconds)
    tol = timedelta(seconds=poll)
    return conn.execute(
        "SELECT event_ts, open_interest FROM open_interest WHERE instrument_id = %(id)s"
        " AND event_ts BETWEEN %(lo)s AND %(hi)s AND event_ts <= %(limit)s"
        " ORDER BY abs(extract(epoch FROM event_ts - %(t)s)), event_ts LIMIT 1",
        {"id": iid, "lo": target - tol, "hi": target + tol, "t": target,
         "limit": base["event_ts"] - timedelta(seconds=seconds / 2)}).fetchone()


def oi_state(conn: psycopg.Connection, inst: dict, frm: datetime, to: datetime) -> Optional[dict]:
    if inst["market_type"] == "spot":
        return None
    iid = inst["instrument_id"]
    base = conn.execute("SELECT event_ts, open_interest FROM open_interest WHERE instrument_id = %s AND event_ts >= %s"
                        " AND event_ts < %s ORDER BY event_ts DESC LIMIT 1", [iid, frm, to]).fetchone()
    if base is None:
        return None
    poll = oi_poll_s(conn, iid, to)
    changes = {}
    for key, secs in (("change_5m_pct", 300), ("change_1h_pct", 3600)):
        ref = oi_at_distance(conn, iid, base, secs, poll)
        changes[key] = pct2(base["open_interest"], ref["open_interest"]) if ref else None
    step = max(int(poll), -(-int((to - frm).total_seconds()) // OI_MAX_POINTS))
    series = conn.execute(
        "SELECT DISTINCT ON (date_bin(make_interval(secs => %(step)s), event_ts, %(origin)s)) event_ts, open_interest"
        " FROM open_interest WHERE instrument_id = %(id)s AND event_ts >= %(frm)s AND event_ts < %(to)s"
        " ORDER BY date_bin(make_interval(secs => %(step)s), event_ts, %(origin)s), event_ts DESC",
        {"step": step, "origin": ORIGIN, "id": iid, "frm": frm, "to": to}).fetchall()
    unit = "contracts" if inst["contract_type"] == "inverse" else inst["base_asset"]
    return {"value": dstr(base["open_interest"]), "unit": unit, "at": iso(base["event_ts"]), **changes,
            "series": [{"t": iso(r["event_ts"]), "value": dstr(r["open_interest"])} for r in series]}


def funding_state(conn: psycopg.Connection, inst: dict, frm: datetime, to: datetime) -> Optional[dict]:
    if inst["market_type"] == "spot":
        return None
    m = last_mark(conn, inst["instrument_id"], to, frm)
    if m is None or m["funding_rate"] is None:
        return None
    # the mark-price stream carries the predicted rate of the next settlement; no settlement is recorded yet
    return {"rate_pct": rate_pct(m["funding_rate"]), "kind": "estimated", "next_at": iso(m["next_funding_ts"]),
            "at": iso(m["event_ts"])}


def prices(conn: psycopg.Connection, inst: dict, frm: datetime, to: datetime) -> List[dict]:
    lt = last_trade(conn, inst["instrument_id"], to, frm)
    m = last_mark(conn, inst["instrument_id"], to, frm) if inst["market_type"] != "spot" else None
    idx = m["index_price"] if m else None
    return [{"type": "last_trade", "value": dstr(lt["price"]) if lt else None, "at": iso(lt["event_ts"]) if lt else None},
            {"type": "mark", "value": dstr(m["mark_price"]) if m else None, "at": iso(m["event_ts"]) if m else None},
            {"type": "index", "value": dstr(idx), "at": iso(m["event_ts"]) if idx is not None else None}]


# -- quality ------------------------------------------------------------------------------------
def _newest(conn: psycopg.Connection, iid: int, to: datetime) -> Dict[str, Optional[datetime]]:
    q = conn.execute(
        """
        SELECT (SELECT max(event_ts) FROM trades WHERE instrument_id = %(i)s AND event_ts < %(t)s) AS trades,
               (SELECT max(captured_ts) FROM book_snapshots WHERE instrument_id = %(i)s AND kind = 'bucket_summary'
                  AND captured_ts < %(t)s) AS book,
               (SELECT max(open_time) + interval '60 seconds' FROM klines WHERE instrument_id = %(i)s
                  AND interval_s = 60 AND open_time <= %(t)s - interval '60 seconds') AS kline,
               (SELECT max(event_ts) FROM open_interest WHERE instrument_id = %(i)s AND event_ts < %(t)s) AS oi,
               (SELECT max(event_ts) FROM mark_prices WHERE instrument_id = %(i)s AND event_ts < %(t)s) AS mark,
               (SELECT max(event_ts) FROM liquidations WHERE instrument_id = %(i)s AND event_ts < %(t)s) AS liq
        """, {"i": iid, "t": to}).fetchone()
    return dict(q)


def _outage(g: dict) -> bool:
    return g["stream"] == "connection" or g["reason"] in ("disconnected", "collector_restart")


def quality(conn: psycopg.Connection, inst: dict, to: datetime, cutoff: datetime) -> List[dict]:
    """One row per stream. Precedence: unavailable > disconnected > book_resync > incomplete > stale > ok.

    * disconnected / book_resync: a gap (as of the cut-off) still open at ``to``. Websocket outages
      ('disconnected', 'collector_restart', stream 'connection') apply to every websocket stream of the
      instrument; open interest is polled over REST, so only a restart or its own gaps apply to it.
    * incomplete: any unrecovered gap of the stream overlapping the 5 minutes before ``to``.
    * stale: the newest row is older than 3 x the stream's expected interval before ``to``.
    """
    iid = inst["instrument_id"]
    spot = inst["market_type"] == "spot"
    newest = _newest(conn, iid, to)
    gaps = bad_gaps(gaps_as_of(conn, iid, to - QUALITY_GAP_WINDOW, to, cutoff))
    out: List[dict] = []
    by_name: Dict[str, dict] = {}
    for name, dq in QUALITY_STREAMS:
        last = newest[dq]
        row = {"stream": name, "status": "ok", "last_at": iso(last), "note": None}
        out.append(row)
        by_name[name] = row
        if spot and name in ("oi", "mark", "liquidations"):
            row.update(status="unavailable", last_at=None, note=SPOT_NOTE)
            continue
        mine = [g for g in gaps if g["stream"] == dq or (_outage(g) and (
            name != "oi" or g["reason"] == "collector_restart"))]
        still_open = [g for g in mine if g["gap_end"] is None or _ts(g["gap_end"]) > to]
        down = [g for g in still_open if _outage(g)]
        resync = [g for g in still_open if name == "book" and g["reason"] == "book_resync"]
        if name == "liquidations":
            mark = by_name["mark"]
            if last is None and mark["status"] == "unavailable":
                row.update(status="unavailable", note="ยังไม่มีข้อมูล liquidation หรือ mark price ในฐานข้อมูลก่อนเวลา to")
                continue
        elif last is None:
            row.update(status="unavailable", note="ยังไม่มีข้อมูลของสตรีมนี้ในฐานข้อมูลก่อนเวลา to")
            continue
        if down:
            g = min(down, key=lambda x: x["gap_start"])
            row.update(status="disconnected", note=f"การเชื่อมต่อขาดตั้งแต่ {g['gap_start']} และยังไม่กลับมา ณ เวลา to"
                                                   f" ({g['reason']})")
        elif resync:
            row.update(status="book_resync", note=f"order book กำลัง resync ตั้งแต่ {resync[0]['gap_start']}")
        elif mine:
            reasons = ", ".join(sorted({g["reason"] for g in mine}))
            row.update(status="incomplete", note=f"มีช่วงข้อมูลขาดที่ยังไม่ได้คืนใน 5 นาทีก่อน to ({reasons})")
        elif name == "liquidations":
            mark = by_name["mark"]
            if mark["status"] == "stale":
                row.update(status="stale", note="สตรีม mark price ที่มาทางการเชื่อมต่อเดียวกันไม่มีข้อมูลใหม่")
            else:
                row["note"] = ("liquidation เกิดเป็นครั้งคราว ไม่มีรอบเวลาคงที่: ช่วงที่ไม่มีรายการไม่ได้แปลว่าข้อมูลขาด;"
                               " สถานะอิงสตรีม mark price ที่มาทางการเชื่อมต่อเดียวกัน")
        else:
            interval = EXPECTED_S[name] if name in EXPECTED_S else spacing_s(conn, dq, iid, to)
            age = (to - last).total_seconds()
            if age > STALE_FACTOR * interval:
                row.update(status="stale", note=f"แถวล่าสุดเก่ากว่า to {int(age)} วินาที (เกณฑ์ {STALE_FACTOR} เท่าของรอบปกติ"
                                                f" {interval:g} วินาที)")
    return out


# -- observations -------------------------------------------------------------------------------
def observations(conn: psycopg.Connection, inst: dict, to: datetime, cutoff: datetime) -> List[dict]:
    """Facts at ``to``, at most five, most notable first. Each item is omitted, never zero-filled,
    when its inputs are missing. Notability score (higher first, ties in OBS_ORDER), each scaled so
    1 is "clearly unusual": cvd |delta| / known-side volume; oi |change %| / 0.5; wall (mult - 4) / 6;
    bigtrade |net| / big-trade volume; funding |rate %| / 0.03."""
    iid = inst["instrument_id"]
    base, quote_asset = inst["base_asset"], inst["quote_asset"]
    spot = inst["market_type"] == "spot"
    items: List[Tuple[float, dict]] = []

    # CVD over 5 minutes from the trade tape (unknown side excluded, nothing if the tape has a gap)
    start = to - timedelta(seconds=OBS_CVD_S)
    if not trade_gaps(conn, iid, start, to, cutoff):
        r = conn.execute("SELECT sum(qty) FILTER (WHERE taker_side = 'buy') AS buy,"
                         " sum(qty) FILTER (WHERE taker_side = 'sell') AS sell FROM trades"
                         " WHERE instrument_id = %s AND event_ts >= %s AND event_ts < %s", [iid, start, to]).fetchone()
        if r["buy"] is not None or r["sell"] is not None:
            buy, sell = r["buy"] or Decimal(0), r["sell"] or Decimal(0)
            p0, p1 = first_last_price(conn, iid, start, to)
            delta = buy - sell
            items.append((_score(float(abs(delta) / (buy + sell))), {
                "kind": "cvd", "at": iso(to), "window_s": OBS_CVD_S, "evidence": "chart",
                "values": {"cvd_change": dstr(delta), "price_change_pct": pct2(p1, p0)},
                "units": {"cvd_change": base}}))

    # open interest change over 5 minutes, with the price change between the same two readings
    if not spot:
        poll = oi_poll_s(conn, iid, to)
        cur = conn.execute("SELECT event_ts, open_interest FROM open_interest WHERE instrument_id = %s AND event_ts < %s"
                           " AND event_ts >= %s ORDER BY event_ts DESC LIMIT 1",
                           [iid, to, to - timedelta(seconds=STALE_FACTOR * poll)]).fetchone()
        ref = oi_at_distance(conn, iid, cur, OBS_OI_S, poll) if cur else None
        if cur and ref:
            oi_pct = pct2(cur["open_interest"], ref["open_interest"])
            p0, p1 = first_last_price(conn, iid, ref["event_ts"], cur["event_ts"] + timedelta(microseconds=1))
            if oi_pct is not None and p0 is not None:
                items.append((_score(abs(float(oi_pct)) / 0.5), {
                    "kind": "oi", "at": iso(cur["event_ts"]), "window_s": OBS_OI_S, "evidence": "oi",
                    "values": {"oi_change_pct": oi_pct, "price_change_pct": pct2(p1, p0)}, "units": {}}))

    # the largest wall on the book now
    book = book_state(conn, inst, to, cutoff)
    if book and book["top"]:
        w = book["top"]
        items.append((_score((float(w["mult"]) - WALL_PARAMS["min_mult"]) / 6.0), {
            "kind": "wall", "at": book["at"], "window_s": None, "evidence": "walls",
            "values": {"side": w["side"], "price": w["price"], "size": w["size"], "mult": w["mult"],
                       "age_s": str(w["age_s"])},
            "units": {"price": quote_asset, "size": base}}))

    # big trades over 30 minutes (threshold from the 4 h before the window, as in /details)
    start = to - timedelta(seconds=OBS_BIG_S)
    thr, _, _, _ = big_threshold(conn, iid, start, to)
    if thr is not None and not trade_gaps(conn, iid, start, to, cutoff):
        r = conn.execute(
            "SELECT count(*) FILTER (WHERE taker_side = 'buy') AS nb, count(*) FILTER (WHERE taker_side = 'sell') AS ns,"
            " coalesce(sum(qty) FILTER (WHERE taker_side = 'buy'), 0) AS vb,"
            " coalesce(sum(qty) FILTER (WHERE taker_side = 'sell'), 0) AS vs"
            " FROM trades WHERE instrument_id = %s AND event_ts >= %s AND event_ts < %s AND qty >= %s",
            [iid, start, to, thr]).fetchone()
        if r["nb"] + r["ns"] > 0:
            net = r["vb"] - r["vs"]
            items.append((_score(float(abs(net) / (r["vb"] + r["vs"]))), {
                "kind": "bigtrade", "at": iso(to), "window_s": OBS_BIG_S, "evidence": "walls",
                "values": {"buy_count": str(r["nb"]), "sell_count": str(r["ns"]), "net_size": dstr(net),
                           "threshold": dstr(thr)},
                "units": {"net_size": base}}))

    # funding: the predicted rate on the newest mark row of the last 15 minutes
    if not spot:
        m = last_mark(conn, iid, to, to - OBS_FUNDING_MAX_AGE)
        if m and m["funding_rate"] is not None:
            rp = rate_pct(m["funding_rate"])
            items.append((_score(abs(float(rp)) / 0.03), {
                "kind": "funding", "at": iso(m["event_ts"]), "window_s": None, "evidence": "oi",
                "values": {"rate_pct": rp, "kind": "estimated"}, "units": {}}))

    items.sort(key=lambda x: (-x[0], OBS_ORDER.index(x[1]["kind"])))
    return [d for _, d in items[:5]]
