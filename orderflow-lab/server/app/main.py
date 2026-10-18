"""Flow Lab API (FastAPI).

    uvicorn --factory app.main:create_app --host 127.0.0.1 --port 8080 --no-access-log

The browser/PWA talks only to this API. The API never calls an exchange, a trading API
or a claude.ai connector: market data comes from the database filled by ingest.py.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import errors
from .config import API_VERSION, Settings
from .db import Database
from .routers import alerts, export, journal, market, market_state, me, trades

log = logging.getLogger("flowlab.access")

# query parameters that look like credentials are refused: tokens belong in the
# Authorization header, never in URLs (they end up in proxy/access logs and history)
CREDENTIAL_PARAMS = {"access_token", "token", "id_token", "refresh_token", "api_key", "apikey", "key", "jwt",
                     "password", "secret", "auth", "authorization", "signature"}

DESCRIPTION = """\
Backend for the Flow Lab PWA: a market-analytics and private-journal tool.
It gives no entry/stop-loss/take-profit recommendations.

* Authentication: `Authorization: Bearer <JWT>` only. Credentials in the URL are rejected.
* Market data is read from our database; members with the *delayed* entitlement only receive
  data older than now - 15 minutes on every market endpoint (enforced server-side).
* Decimals are strings. Timestamps are ISO-8601 UTC (`Z`); request timestamps must carry a timezone.
* Market data shown to members requires a data licence from the source (see ROADMAP section 4).
"""


def _configure_logging() -> None:
    """uvicorn configures only its own loggers; give ours a handler unless the host set one up."""
    ours = logging.getLogger("flowlab")
    if not ours.handlers and not logging.getLogger().handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        ours.addHandler(h)
    if ours.level == logging.NOTSET:
        ours.setLevel(logging.INFO)


def create_app(settings: Optional[Settings] = None, *, connect: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    _configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = None
        if connect:
            settings.validate()
            db = Database(settings.database_url, settings.pool_min, settings.pool_max)
            db.open()
            try:
                db.check_role()
            except Exception:
                db.close()
                raise
            app.state.db = db
        try:
            yield
        finally:
            if db is not None:
                db.close()

    app = FastAPI(title="Flow Lab API", version=API_VERSION, description=DESCRIPTION, lifespan=lifespan,
                  openapi_tags=[{"name": "account"}, {"name": "market data"}, {"name": "journal"},
                                {"name": "personal trades"}, {"name": "alerts"}, {"name": "export"}])
    app.state.settings = settings
    app.state.db = None
    errors.install(app)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        rid = uuid.uuid4().hex[:16]
        start = time.monotonic()
        bad = sorted(k for k in request.query_params.keys() if k.lower() in CREDENTIAL_PARAMS)
        if bad:
            resp = JSONResponse(errors.body("credentials_in_url",
                                            "credentials must be sent in the Authorization header, not the URL",
                                            {"parameters": bad}), status_code=400)
        else:
            resp = await call_next(request)
        if request.url.path.startswith("/v1"):
            # member data must not stay in shared/browser caches after logout or account switch
            resp.headers["Cache-Control"] = "no-store, private"
            resp.headers["Vary"] = "Authorization"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Request-Id"] = rid
        # path only: no query string, no headers, no bodies
        log.info("%s %s %s %.1fms rid=%s", request.method, request.url.path, resp.status_code,
                 (time.monotonic() - start) * 1000, rid)
        return resp

    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                           allow_methods=["GET", "POST", "PATCH", "DELETE"],
                           allow_headers=["Authorization", "Content-Type", "Idempotency-Key"])

    @app.get("/healthz", tags=["account"], summary="Liveness (no authentication, no data)")
    def healthz():
        return {"status": "ok", "version": API_VERSION}

    for r in (me.router, market.router, market_state.router, journal.router, trades.router, alerts.router, export.router):
        app.include_router(r)
    return app

