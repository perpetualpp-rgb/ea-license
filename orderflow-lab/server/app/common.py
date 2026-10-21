"""Shared request context, decimal types, cursors and entitlement resolution."""
from __future__ import annotations

import base64
import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Iterator, Optional, Tuple
from uuid import UUID

import psycopg
from fastapi import Depends, Request
from pydantic import BeforeValidator, PlainSerializer, WithJsonSchema

from .auth import Principal, get_principal
from .config import Settings
from .db import Database
from .errors import ApiError

UTC = timezone.utc


# -- decimals ----------------------------------------------------------------------
def _to_decimal(v: Any) -> Decimal:
    if isinstance(v, bool):
        raise ValueError("expected a decimal number")
    if isinstance(v, float):
        v = repr(v)  # shortest round-trip text; send strings to be exact
    if isinstance(v, int):
        v = str(v)
    if isinstance(v, str):
        try:
            v = Decimal(v.strip())
        except InvalidOperation:
            raise ValueError("expected a decimal number such as \"0.015\"") from None
    if not isinstance(v, Decimal):
        raise ValueError("expected a decimal number")
    if not v.is_finite():
        raise ValueError("must be a finite number")
    return v


def dstr(d: Optional[Decimal]) -> Optional[str]:
    """Plain decimal text without exponent; None stays None (no value is not zero)."""
    if d is None:
        return None
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


DecimalIn = Annotated[
    Decimal, BeforeValidator(_to_decimal), PlainSerializer(dstr, return_type=str),
    WithJsonSchema({"type": "string", "pattern": r"^-?\d+(\.\d+)?$",
                    "description": "decimal number as a string (JSON numbers are accepted, strings are exact)"}),
]
DecimalOut = Annotated[Optional[str], WithJsonSchema(
    {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "decimal as a string; null = no data"})]


def canonical_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


# -- time ---------------------------------------------------------------------------
def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(t: Optional[datetime]) -> Optional[str]:
    return None if t is None else t.astimezone(UTC).isoformat().replace("+00:00", "Z")


# -- cursors -----------------------------------------------------------------------
def encode_cursor(ts: datetime, ident: Any) -> str:
    raw = json.dumps([ts.astimezone(UTC).isoformat(), str(ident)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> Tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        ts, ident = json.loads(raw)
        t = datetime.fromisoformat(ts)
        if t.tzinfo is None:
            raise ValueError
        return t, ident
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ApiError(400, "invalid_cursor", "cursor is not valid") from None


# -- entitlement -------------------------------------------------------------------
@dataclass(frozen=True)
class MarketAccess:
    level: str                 # 'realtime' | 'delayed' | 'none'
    now: datetime
    cutoff: datetime           # market data must be older than this
    delay_minutes: int
    valid_until: Optional[datetime] = None

    def as_dict(self) -> dict:
        return {"level": self.level, "delay_minutes": self.delay_minutes if self.level == "delayed" else 0,
                "data_until": iso(self.cutoff), "valid_until": iso(self.valid_until)}


def resolve_access(conn: psycopg.Connection, settings: Settings, now: Optional[datetime] = None) -> MarketAccess:
    now = now or utcnow()
    row = conn.execute(
        "SELECT level, valid_until FROM entitlements WHERE product = 'market_data' AND valid_from <= %s"
        " AND (valid_until IS NULL OR valid_until > %s) ORDER BY (level = 'realtime') DESC, valid_until DESC NULLS FIRST"
        " LIMIT 1", [now, now]).fetchone()
    level = row["level"] if row else settings.default_entitlement
    if level == "realtime":
        return MarketAccess("realtime", now, now, 0, row["valid_until"])
    if level == "delayed":
        return MarketAccess("delayed", now, now - timedelta(minutes=settings.delay_minutes), settings.delay_minutes,
                            row["valid_until"] if row else None)
    return MarketAccess("none", now, now, 0)


def require_market_access(access: MarketAccess) -> MarketAccess:
    if access.level == "none":
        raise ApiError(403, "no_market_data_entitlement", "market data is not included in this membership")
    return access


# -- request context ------------------------------------------------------------------
@dataclass
class Ctx:
    principal: Principal
    db: Database
    settings: Settings

    @property
    def user_id(self) -> UUID:
        return self.principal.user_id

    @contextmanager
    def tx(self) -> Iterator[psycopg.Connection]:
        p = self.principal
        with self.db.user_tx(p.user_id, p.issuer, p.subject, p.email) as conn:
            yield conn


def get_ctx(request: Request, principal: Principal = Depends(get_principal)) -> Ctx:
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise ApiError(503, "database_unavailable", "database not configured")
    return Ctx(principal, db, request.app.state.settings)


def check_range(frm: Optional[datetime], to: Optional[datetime]) -> None:
    for t in (frm, to):
        if t is not None and t.tzinfo is None:
            raise ApiError(422, "timezone_required", "timestamps must include a timezone, e.g. 2026-10-18T03:00:00Z")
    if frm is not None and to is not None and to <= frm:
        raise ApiError(422, "invalid_range", "'to' must be after 'from'")
