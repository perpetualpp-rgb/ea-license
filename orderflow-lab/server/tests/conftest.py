"""Session fixtures: one throwaway PostgreSQL cluster, migrated once, plus API/ingest roles.

Tests that need the database are skipped (with the reason) when initdb/pg_ctl are missing.
Each test creates its own members and instruments, so tests do not depend on each other.
"""
from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional

import psycopg
import pytest
from psycopg.rows import dict_row

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent
for p in (str(SERVER), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import migrate  # noqa: E402
from app.auth import user_id_for  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import CONNECT_OPTIONS  # noqa: E402
from app.devtoken import mint  # noqa: E402
from pgcluster import ClusterUnavailable, PgCluster  # noqa: E402

SECRET = "test-only-secret-" + "s" * 40
UTC = timezone.utc


@pytest.fixture(scope="session")
def pg():
    try:
        cluster = PgCluster().start()
    except ClusterUnavailable as e:
        pytest.skip(f"PostgreSQL test cluster unavailable: {e}")
    yield cluster
    cluster.stop()


@pytest.fixture(scope="session")
def dsns(pg) -> Dict[str, object]:
    with psycopg.connect(pg.dsn(), autocommit=True) as conn:
        conn.execute("CREATE DATABASE flowlab_test")
    admin = pg.dsn(dbname="flowlab_test")
    applied = migrate.apply_migrations(admin)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("CREATE ROLE flowlab_api LOGIN IN ROLE flowlab_app")
        conn.execute("CREATE ROLE flowlab_ingestor LOGIN IN ROLE flowlab_ingest")
    return {"admin": admin, "api": pg.dsn("flowlab_api", "flowlab_test"),
            "ingest": pg.dsn("flowlab_ingestor", "flowlab_test"), "applied": applied, "cluster": pg}


@pytest.fixture(scope="session")
def settings(dsns) -> Settings:
    return Settings(database_url=dsns["api"], jwt_secret=SECRET, env="test")


@pytest.fixture(scope="session")
def app(settings):
    from app.main import create_app
    return create_app(settings)


@pytest.fixture(scope="session")
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


def _connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row, options=CONNECT_OPTIONS)


@pytest.fixture
def admin(dsns):
    conn = _connect(dsns["admin"])
    yield conn
    conn.close()


@pytest.fixture
def app_conn(dsns):
    """Direct connection as the API login role (RLS applies)."""
    conn = _connect(dsns["api"])
    yield conn
    conn.close()


@dataclass
class Member:
    sub: str
    id: uuid.UUID
    token: str

    @property
    def h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}


@pytest.fixture
def member(settings, client):
    """Factory: a new member, provisioned through GET /v1/me."""
    def make(realtime: bool = False, admin_conn: Optional[psycopg.Connection] = None) -> Member:
        sub = f"member-{uuid.uuid4().hex[:12]}"
        m = Member(sub, user_id_for(settings.jwt_issuer, sub), mint(settings, sub))
        assert client.get("/v1/me", headers=m.h).status_code == 200
        if realtime:
            assert admin_conn is not None
            admin_conn.execute("INSERT INTO entitlements (owner_id, product, level, source) VALUES (%s, 'market_data',"
                               " 'realtime', 'test')", [m.id])
        return m
    return make


@pytest.fixture
def instrument(admin):
    """Factory: a fresh instrument (unique symbol) for one test."""
    def make(market_type: str = "perpetual", base: str = "BTC", quote: str = "USDT") -> dict:
        symbol = f"{base}{uuid.uuid4().hex[:6].upper()}{quote}"
        contract = "spot" if market_type == "spot" else "linear"
        return admin.execute(
            "INSERT INTO instruments (venue, symbol, market_type, contract_type, base_asset, quote_asset,"
            " settlement_asset, contract_multiplier) VALUES ('testvenue', %s, %s, %s, %s, %s, %s, 1) RETURNING *",
            [symbol, market_type, contract, base, quote, quote]).fetchone()
    return make


def now_utc() -> datetime:
    return datetime.now(UTC)


def ts(s: Optional[str]) -> Optional[datetime]:
    return None if s is None else datetime.fromisoformat(s.replace("Z", "+00:00"))


def floor_minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def seed_market(admin: psycopg.Connection, inst: dict, minutes: int = 90, end: Optional[datetime] = None,
                trade_every_s: int = 30, book: bool = True) -> dict:
    """1m klines and trades up to ``end`` (default: now) for a fresh instrument
    (+ a one-level book every 10 s unless ``book`` is False)."""
    end = end or now_utc()
    iid = inst["instrument_id"]
    start = floor_minute(end) - timedelta(minutes=minutes)
    k_rows, t_rows = [], []
    t = start
    i = 0
    while t + timedelta(minutes=1) <= end:
        price = Decimal("100") + Decimal(i % 7)
        k_rows.append((iid, 60, t, price, price + 1, price - 1, price + Decimal("0.5"), Decimal("10.5"), 20,
                       Decimal("6.25")))
        t += timedelta(minutes=1)
        i += 1
    agg = 1
    t = start
    while t < end:
        side = ["buy", "sell", "unknown"][agg % 3]
        t_rows.append((inst["venue"], inst["market_type"], inst["symbol"], agg, iid, t,
                       Decimal("100.1") + Decimal(agg % 5) / 10, Decimal("0.125"), side))
        agg += 1
        t += timedelta(seconds=trade_every_s)
    with admin.cursor() as cur:
        cur.executemany("INSERT INTO klines (instrument_id, interval_s, open_time, open, high, low, close, volume,"
                        " trade_count, taker_buy_volume) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", k_rows)
        cur.executemany("INSERT INTO trades (venue, market_type, symbol, agg_trade_id, instrument_id, event_ts, price,"
                        " qty, taker_side) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", t_rows)
        rows = []
        bt = start
        while book and bt < end:
            rows.append((iid, bt, Decimal("100.5"), '[["100","1.5"]]', '[["101","2.5"]]'))
            bt += timedelta(seconds=10)
        cur.executemany("INSERT INTO book_snapshots (instrument_id, captured_ts, mid, bids, asks, synced)"
                        " VALUES (%s, %s, %s, %s, %s, true)", rows)
    return {"start": start, "end": end, "klines": len(k_rows), "trades": len(t_rows)}


def seed_futures_state(admin: psycopg.Connection, inst: dict, start: datetime, end: datetime,
                       mark_every_s: int = 5, oi_every_s: int = 60) -> dict:
    """Synthetic mark/index/funding rows every ``mark_every_s`` and open interest every ``oi_every_s``
    in [start, end): mark 100.2 + (i % 5) / 100, index = mark - 0.05, funding 0.0001 (next settlement
    at the following 8-hour boundary), OI 1000 + 2 * i."""
    iid = inst["instrument_id"]
    marks, ois = [], []
    t, i = start, 0
    while t < end:
        mark = Decimal("100.2") + Decimal(i % 5) / 100
        nxt = t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=8 - t.hour % 8)
        marks.append((iid, t, mark, mark - Decimal("0.05"), Decimal("0.0001"), nxt))
        t += timedelta(seconds=mark_every_s)
        i += 1
    t, i = start, 0
    while t < end:
        ois.append((iid, t, Decimal(1000 + 2 * i)))
        t += timedelta(seconds=oi_every_s)
        i += 1
    with admin.cursor() as cur:
        cur.executemany("INSERT INTO mark_prices (instrument_id, event_ts, mark_price, index_price, funding_rate,"
                        " next_funding_ts) VALUES (%s, %s, %s, %s, %s, %s)", marks)
        cur.executemany("INSERT INTO open_interest (instrument_id, event_ts, open_interest) VALUES (%s, %s, %s)", ois)
    return {"marks": len(marks), "oi": len(ois)}


def seed_wall_book(admin: psycopg.Connection, inst: dict, t0: datetime, minutes: int = 30, every_s: int = 2,
                   hole: Optional[tuple] = (120, 180)) -> datetime:
    """Book bucket summaries (bucket 0.1, ten levels a side of size 1) every ``every_s`` from ``t0``,
    with scripted walls; returns the end time. ``hole`` = seconds after t0 without snapshots.

    A  bid 99.5 x 20   the whole time                  -> active, holding
    E  ask 100.6 x 8   the whole time, 4.5 at the end   -> active, pulling
    B  ask 100.8 x 15  until +10 min, price stays away  -> event removed
    C  bid 99.8 x 12   until +15 min, then the book drops so the best bid is 99.8 -> event price_reached
    the hole ends every wall tracked before it          -> events unknown
    """
    iid = inst["instrument_id"]
    end = t0 + timedelta(minutes=minutes)
    step = timedelta(seconds=every_s)
    rows = []
    t = t0
    while t < end:
        s = (t - t0).total_seconds()
        if hole and hole[0] <= s < hole[1]:
            t += step
            continue
        shifted = s >= 15 * 60
        best = Decimal("99.8") if shifted else Decimal("100.0")
        bids = {best - Decimal("0.1") * k: Decimal(1) for k in range(10)}
        asks = {best + Decimal("0.1") * (k + 1): Decimal(1) for k in range(10)}
        bids[Decimal("99.5")] = Decimal(20)
        asks[Decimal("100.6")] = Decimal(8) if t + step < end else Decimal("4.5")
        if s < 600:
            asks[Decimal("100.8")] = Decimal(15)
        if not shifted:
            bids[Decimal("99.8")] = Decimal(12)
        b = json.dumps([[str(p), str(q)] for p, q in sorted(bids.items(), reverse=True)])
        a = json.dumps([[str(p), str(q)] for p, q in sorted(asks.items())])
        rows.append((iid, t, best + Decimal("0.05"), b, a))
        t += step
    with admin.cursor() as cur:
        cur.executemany("INSERT INTO book_snapshots (instrument_id, captured_ts, mid, bids, asks, synced)"
                        " VALUES (%s, %s, %s, %s, %s, true)", rows)
    return end
