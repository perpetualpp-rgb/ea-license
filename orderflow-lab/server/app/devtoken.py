"""Mint a development HS256 token (dev/test only; production tokens come from the identity provider).

    FLOWLAB_JWT_SECRET=... python3 -m app.devtoken --sub alice --minutes 60
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Optional

import jwt

from .config import Settings


def mint(settings: Settings, sub: str, minutes: float = 60, email: Optional[str] = None, **extra) -> str:
    now = int(time.time())
    claims = {"sub": sub, "iss": settings.jwt_issuer, "aud": settings.jwt_audience, "iat": now,
              "exp": now + int(minutes * 60), **extra}
    if email:
        claims["email"] = email
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="mint a development access token")
    ap.add_argument("--sub", required=True)
    ap.add_argument("--email")
    ap.add_argument("--minutes", type=float, default=60)
    args = ap.parse_args(argv)
    s = Settings.from_env()
    if s.env == "production" or len(s.jwt_secret.encode()) < 32:
        print("error: FLOWLAB_JWT_SECRET (>= 32 bytes) required and FLOWLAB_ENV must not be production", file=sys.stderr)
        return 1
    print(mint(s, args.sub, args.minutes, args.email))
    return 0


if __name__ == "__main__":
    sys.exit(main())
