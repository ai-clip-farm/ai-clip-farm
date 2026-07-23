"""API authentication + rate limiting.

Auth: a single shared API key (`X-API-Key` header or `Authorization: Bearer`),
checked with `secrets.compare_digest` to avoid timing attacks. This is
intentionally simple — a hosted multi-tenant SaaS would swap this for
per-user OAuth/JWT, but for a self-hosted "runs every day, unattended" system,
one strong key you rotate is the right amount of complexity. Disabled entirely
when `API_KEY` is empty (local dev), and refused at startup in production
config (see `Settings._validate_production_invariants`).

Rate limiting: slowapi (a Flask-limiter-style wrapper over `limits`), backed
by Redis so limits are shared correctly across multiple API replicas instead
of each process keeping its own in-memory counter.
"""

from __future__ import annotations

import secrets as _secrets

from fastapi import Header, HTTPException, status
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import settings


def _api_key_from_headers(x_api_key: str | None, authorization: str | None) -> str | None:
    if x_api_key:
        return x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency — raises 401 if auth is enabled and the key is wrong
    or missing. A no-op when `API_KEY` is unset (local development)."""
    if not settings.auth_enabled:
        return
    provided = _api_key_from_headers(x_api_key, authorization)
    if not provided or not _secrets.compare_digest(provided, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _rate_limit_key(request) -> str:
    """Rate-limit per API key when auth is enabled (fairer under a shared
    NAT/proxy), otherwise per client IP."""
    if settings.auth_enabled:
        key = _api_key_from_headers(
            request.headers.get("x-api-key"), request.headers.get("authorization")
        )
        if key:
            return f"key:{key[:12]}"
    return get_remote_address(request)


limiter = Limiter(
    key_func=_rate_limit_key,
    storage_uri=settings.redis_url,
    default_limits=[f"{settings.rate_limit_per_minute}/minute"],
    headers_enabled=True,
    swallow_errors=True,  # if Redis is briefly unavailable, fail OPEN not closed
)
