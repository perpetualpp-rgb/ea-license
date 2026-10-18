#!/usr/bin/env python3
"""Local development stack in one process: a throwaway PostgreSQL, the migrations, the
collector's files loaded by ingest (again every --follow seconds), and the API.

    python3 devstack.py --data ../data/live --collector-config ../live/config.json \
        --port 8080 --cors http://localhost:5173 --follow 15

It prints the API URL and a development token for a member with realtime access; paste
both into the app (ตั้งค่า → แหล่งข้อมูลตลาด → เซิร์ฟเวอร์ Flow Lab). Ctrl-C stops everything and
deletes the database. Development only: trust auth on 127.0.0.1, HS256 token, no TLS.
"""
from __future__ import annotations

import argparse
import json
import secrets
import signal
import sys
import threading
import time
import urllib.request
from pathlib import Path

import psycopg
import uvicorn
from psycopg.rows import dict_row

HERE = Path(__file__).resolve().parent
for p in (HERE, HERE / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import migrate  # noqa: E402
from app.auth import user_id_for  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import CONNECT_OPTIONS  # noqa: E402
from app.devtoken import mint  # noqa: E402
from app.main import create_app  # noqa: E402
from ingest import Ingestor  # noqa: E402
from pgcluster import PgCluster  # noqa: E402

OWNER_SUB = "owner-dev"


def ingest_once(dsn: str, data: Path, book_cfg: dict) -> dict:
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row, options=CONNECT_OPTIONS) as conn:
        return dict(Ingestor(conn, data, "binance", book_cfg).run())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="throwaway PostgreSQL + ingest + Flow Lab API for local testing")
    ap.add_argument("--data", required=True, help="collector data_dir")
    ap.add_argument("--collector-config", help="collector config, for book bucket size and window")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--cors", default="http://localhost:5173", help="comma-separated app origins")
    ap.add_argument("--follow", type=float, default=0, help="re-run ingest every N seconds (0 = once)")
    ap.add_argument("--delayed", action="store_true", help="give the dev member delayed access instead of realtime")
    ap.add_argument("--token-minutes", type=float, default=240)
    ap.add_argument("--out", help="write {api, token} as JSON to this file once the API answers")
    args = ap.parse_args(argv)

    book_cfg = {}
    if args.collector_config:
        book_cfg = json.loads(Path(args.collector_config).read_text(encoding="utf-8")).get("book", {})

    pg = PgCluster().start()
    server = None
    try:
        with psycopg.connect(pg.dsn(), autocommit=True) as conn:
            conn.execute("CREATE DATABASE flowlab")
        admin = pg.dsn(dbname="flowlab")
        migrate.apply_migrations(admin)
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute("CREATE ROLE flowlab_api LOGIN IN ROLE flowlab_app")
            conn.execute("CREATE ROLE flowlab_ingestor LOGIN IN ROLE flowlab_ingest")
        ingest_dsn = pg.dsn("flowlab_ingestor", "flowlab")
        stats = ingest_once(ingest_dsn, Path(args.data), book_cfg)
        print("ingest:", json.dumps(stats, sort_keys=True), flush=True)

        settings = Settings(database_url=pg.dsn("flowlab_api", "flowlab"), jwt_secret=secrets.token_urlsafe(48),
                            env="dev", cors_origins=[o.strip() for o in args.cors.split(",") if o.strip()])
        settings.validate()
        server = uvicorn.Server(uvicorn.Config(create_app(settings), host=args.host, port=args.port,
                                               log_level="warning", access_log=False))
        threading.Thread(target=server.run, daemon=True).start()
        api = f"http://{args.host}:{args.port}"
        token = mint(settings, OWNER_SUB, args.token_minutes)
        for _ in range(100):
            try:  # first call provisions the member
                req = urllib.request.Request(api + "/v1/me", headers={"Authorization": f"Bearer {token}"})
                with urllib.request.urlopen(req, timeout=2) as r:
                    if r.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("API did not start")
        if not args.delayed:
            with psycopg.connect(admin, autocommit=True, options=CONNECT_OPTIONS) as conn:
                conn.execute("INSERT INTO entitlements (owner_id, product, level, source) VALUES (%s, 'market_data',"
                             " 'realtime', 'devstack')", [user_id_for(settings.jwt_issuer, OWNER_SUB)])
        print(f"API   {api}\nTOKEN {token}\naccess: {'delayed' if args.delayed else 'realtime'}", flush=True)
        if args.out:
            Path(args.out).write_text(json.dumps({"api": api, "token": token}), encoding="utf-8")

        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        try:
            while not stop.wait(args.follow or 3600):
                if args.follow:
                    s = ingest_once(ingest_dsn, Path(args.data), book_cfg)
                    print("ingest:", json.dumps({k: v for k, v in s.items() if v}, sort_keys=True), flush=True)
        except KeyboardInterrupt:
            pass
        return 0
    finally:
        if server is not None:
            server.should_exit = True
            time.sleep(0.5)
        pg.stop()


if __name__ == "__main__":
    sys.exit(main())
