"""Settings from environment variables (never from the request, never logged)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

ENGINE_VERSION = "flowlab-server/0.1.0"
SNAPSHOT_SCHEMA_VERSION = 1
API_VERSION = "0.1.0"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    database_url: str = ""
    jwt_secret: str = field(default="", repr=False)
    jwt_issuer: str = "flowlab-dev"
    jwt_audience: str = "flowlab-api"
    env: str = "dev"                       # dev | test | production
    delay_minutes: int = 15                # delayed entitlement lag
    default_entitlement: str = "delayed"   # level for members without an entitlement row
    cors_origins: List[str] = field(default_factory=list)
    pool_min: int = 1
    pool_max: int = 10
    alert_max_live_lag_s: int = 120        # older observations are treated as backfill

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "Settings":
        e = os.environ if env is None else env
        origins = [o.strip() for o in e.get("FLOWLAB_CORS_ORIGINS", "").split(",") if o.strip()]
        return cls(
            database_url=e.get("FLOWLAB_DATABASE_URL", ""),
            jwt_secret=e.get("FLOWLAB_JWT_SECRET", ""),
            jwt_issuer=e.get("FLOWLAB_JWT_ISSUER", "flowlab-dev"),
            jwt_audience=e.get("FLOWLAB_JWT_AUDIENCE", "flowlab-api"),
            env=e.get("FLOWLAB_ENV", "dev"),
            delay_minutes=int(e.get("FLOWLAB_DELAY_MINUTES", "15")),
            default_entitlement=e.get("FLOWLAB_DEFAULT_ENTITLEMENT", "delayed"),
            cors_origins=origins,
            pool_min=int(e.get("FLOWLAB_DB_POOL_MIN", "1")),
            pool_max=int(e.get("FLOWLAB_DB_POOL_MAX", "10")),
        )

    def validate(self) -> None:
        if self.env not in ("dev", "test", "production"):
            raise ConfigError("FLOWLAB_ENV must be dev, test or production")
        if self.env == "production":
            # HS256 with a shared secret is a development stand-in. Production must verify
            # the identity provider's tokens against its JWKS (RS256/ES256), see README.
            raise ConfigError("production needs JWKS verification of the identity provider; "
                              "the HS256 dev secret mode is refused")
        if len(self.jwt_secret.encode()) < 32:
            raise ConfigError("FLOWLAB_JWT_SECRET must be set (at least 32 bytes)")
        if not self.database_url:
            raise ConfigError("FLOWLAB_DATABASE_URL must be set (API role, not the owner)")
        if self.default_entitlement not in ("none", "delayed"):
            raise ConfigError("FLOWLAB_DEFAULT_ENTITLEMENT must be 'none' or 'delayed'")
        if self.delay_minutes < 0:
            raise ConfigError("FLOWLAB_DELAY_MINUTES must be >= 0")
