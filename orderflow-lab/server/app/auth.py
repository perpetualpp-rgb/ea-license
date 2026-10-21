"""Bearer-token authentication.

Development/test mode: HS256 tokens signed with FLOWLAB_JWT_SECRET (see app/devtoken.py).
Production must instead verify the identity provider's tokens (e.g. Supabase Auth or an
OIDC provider fronting LINE/Google) against the provider's JWKS with an asymmetric
algorithm (RS256/ES256), checking iss, aud, exp and nbf; Settings.validate() refuses to
start with FLOWLAB_ENV=production until that verifier exists.

Tokens are accepted only in the Authorization header. They are never read from the URL
(a middleware rejects credential-looking query parameters) and never logged.

The member id is UUIDv5(issuer + "|" + subject): stable per identity provider account,
collision-free across providers, and known before any database lookup so it can be
placed in app.user_id for Row Level Security.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings
from .errors import ApiError

USER_NAMESPACE = uuid.UUID("6c1f0b8e-3c55-5d0e-9a43-2f8f6b0f1a7d")
bearer = HTTPBearer(auto_error=False, description="JWT access token from the identity provider")


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    issuer: str
    subject: str
    email: Optional[str] = None


def user_id_for(issuer: str, subject: str) -> uuid.UUID:
    return uuid.uuid5(USER_NAMESPACE, f"{issuer}|{subject}")


def _unauthorized(code: str, message: str) -> ApiError:
    return ApiError(401, code, message, headers={"WWW-Authenticate": 'Bearer realm="flowlab"'})


def verify_token(token: str, settings: Settings) -> Principal:
    try:
        claims = jwt.decode(
            token, settings.jwt_secret, algorithms=["HS256"], audience=settings.jwt_audience,
            issuer=settings.jwt_issuer, leeway=30,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]})
    except jwt.ExpiredSignatureError:
        raise _unauthorized("token_expired", "access token expired") from None
    except jwt.PyJWTError:
        raise _unauthorized("invalid_token", "access token rejected") from None
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub or len(sub) > 255:
        raise _unauthorized("invalid_token", "access token rejected")
    email = claims.get("email") if isinstance(claims.get("email"), str) else None
    return Principal(user_id=user_id_for(claims["iss"], sub), issuer=claims["iss"], subject=sub, email=email)


def get_principal(request: Request,
                  credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise _unauthorized("missing_token", "Authorization: Bearer <token> required")
    return verify_token(credentials.credentials, request.app.state.settings)
