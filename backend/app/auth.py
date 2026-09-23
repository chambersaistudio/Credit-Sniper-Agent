"""
Authentication and the per-request user identity.

Design
------
The backend does not store passwords or run its own login. A managed auth
provider (Clerk in our deployment; any OIDC/JWKS issuer works) signs a short
JWT for the signed-in user; the frontend sends it as `Authorization: Bearer
<token>`. This module *verifies* that token against the provider's public
keys (JWKS) — signature, issuer, audience, expiry — and maps the token's
stable subject claim to one internal `User` row. Verification uses only
public keys, so no provider secret ever lives on this server, and nothing
sensitive is needed in the frontend beyond the provider's *publishable*
(browser-safe) key.

Two modes (settings.auth_mode):
  jwt      — production/hosted. A valid token is required on every data
             request; identity comes only from the verified token. A
             frontend-supplied user id is never trusted.
  disabled — local development and tests only. Every request resolves to a
             single local user. Must never back a hosted deployment holding
             real data.

`current_user` / `current_user_id` are the dependencies every data route
uses. Ownership of individual records is enforced at each route (see the
`app/api/` modules), not here.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
import uuid
from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.utils.default_user import get_or_create_default_user

# 401s are deliberately vague: they never reveal whether a token was absent,
# malformed, expired, or signed by the wrong key — only that authentication
# failed. WWW-Authenticate lets a client know a bearer token is expected.
_UNAUTHENTICATED = HTTPException(
    status_code=401, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"}
)


class _JwksCache:
    """Caches the provider's signing keys (kid -> RSA public key), refetched
    when a key is unknown or the TTL lapses. Fetching is stdlib-only so the
    backend needs no extra runtime dependency; tests seed `_keys` directly
    and never touch the network."""

    def __init__(self, ttl_seconds: int = 3600):
        self._ttl = ttl_seconds
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def _load(self) -> None:
        if not settings.auth_jwks_url:
            raise RuntimeError("AUTH_JWKS_URL is not configured")
        with urllib.request.urlopen(settings.auth_jwks_url, timeout=5) as resp:  # noqa: S310 (fixed https URL from config)
            document = json.loads(resp.read())
        keys = {}
        for jwk in document.get("keys", []):
            kid = jwk.get("kid")
            if kid and jwk.get("kty") == "RSA":
                keys[kid] = RSAAlgorithm.from_jwk(json.dumps(jwk))
        self._keys = keys
        self._fetched_at = time.monotonic()

    def get(self, kid: str) -> Any:
        with self._lock:
            fresh = (time.monotonic() - self._fetched_at) < self._ttl
            if kid in self._keys and fresh:
                return self._keys[kid]
            # Unknown kid or stale cache: refetch once (keys rotate).
            self._load()
            if kid not in self._keys:
                raise KeyError(kid)
            return self._keys[kid]

    def seed(self, kid: str, public_key: Any) -> None:
        """Test hook: install a key without any network fetch."""
        with self._lock:
            self._keys[kid] = public_key
            self._fetched_at = time.monotonic()


jwks_cache = _JwksCache()


def _verify_token(token: str) -> dict[str, Any]:
    """Verify a bearer JWT and return its claims, or raise the vague 401."""
    try:
        header = jwt.get_unverified_header(token)
        key = jwks_cache.get(header["kid"])
        return jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],
            issuer=settings.auth_issuer or None,
            audience=settings.auth_audience or None,
            options={
                "require": ["exp", "iss", "sub"],
                "verify_aud": bool(settings.auth_audience),
                "verify_iss": bool(settings.auth_issuer),
            },
        )
    except HTTPException:
        raise
    except Exception:
        # Malformed header, unknown/rotated key, bad signature, expired,
        # wrong issuer/audience — all collapse to one opaque failure.
        raise _UNAUTHENTICATED


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _UNAUTHENTICATED
    return token.strip()


async def _provision_user(db: AsyncSession, claims: dict[str, Any]) -> User:
    """Find the user for this token's subject, creating one on first sign-in.
    The subject is the provider's stable id — never an email, which can
    change. Email/name from the token seed the profile but the consumer
    edits them in-app afterward."""
    subject = claims["sub"]
    existing = (
        await db.execute(select(User).where(User.auth_subject == subject))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    email = (claims.get("email") or "").strip()
    user = User(
        id=uuid.uuid4(),
        auth_subject=subject,
        # A placeholder email keeps the NOT NULL/unique column satisfied when
        # the token carries no email; the consumer sets a real one in-app.
        email=email or f"{subject}@auth.local",
        full_name=(claims.get("name") or "").strip(),
    )
    db.add(user)
    await db.flush()
    return user


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """The authenticated user for this request.

    jwt mode: verified strictly from the bearer token. disabled mode: the
    single local user (dev/test only)."""
    if not settings.auth_enabled:
        return await get_or_create_default_user(db)
    token = _bearer_token(request)
    claims = await run_in_threadpool(_verify_token, token)
    return await _provision_user(db, claims)


async def current_user_id(user: User = Depends(current_user)) -> uuid.UUID:
    return user.id
