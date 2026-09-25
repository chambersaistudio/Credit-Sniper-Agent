"""
Who may drive the operator control plane, and what that permission is worth.

Two principals, one surface:

  agent  a machine credential (OPERATOR_AGENT_TOKEN) held by the review/QA
         agent. It exists so production testing can happen without a shell.
  admin  a signed-in user, for the mobile operator page.

The credential is narrow by construction rather than by convention. It is
accepted only by routes under /api/operator/*, and those routes expose a fixed
list of named operations — there is no endpoint here that runs a command,
evaluates code, reads an environment variable or accepts SQL. Holding the
token therefore grants exactly the operations in the registry and nothing
adjacent to them.

The token itself is compared in constant time, is never written to a log, and
is never echoed by any response. Audit records name the principal
("agent:codex", "user:<uuid>") and never any part of the secret.
"""
from __future__ import annotations

import hmac
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Literal

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import User

logger = logging.getLogger(__name__)

# Deliberately vague, exactly like the consumer API's: it never distinguishes
# a missing header from a wrong token, so the endpoint cannot be used to test
# candidate secrets by their error message.
_UNAUTHENTICATED = HTTPException(
    status_code=401, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"}
)
_RATE_LIMITED = HTTPException(status_code=429, detail="Too many operator requests")


@dataclass(frozen=True)
class OperatorPrincipal:
    """The identity behind one operator request."""

    kind: Literal["agent", "admin"]
    label: str
    user_id: uuid.UUID | None = None

    @property
    def audit_name(self) -> str:
        """What is stored on a job. Never any part of a credential."""
        if self.kind == "agent":
            return f"agent:{self.label}"
        return f"user:{self.user_id}"

    @property
    def is_agent(self) -> bool:
        return self.kind == "agent"


def _offered_token(request: Request) -> str | None:
    """The operator credential from this request, if one was offered.

    Accepts a dedicated header or a bearer token. The dedicated header is
    preferred: it keeps the machine credential off the same header a consumer
    JWT uses, so a misrouted request cannot present one as the other."""
    header = request.headers.get("X-Operator-Token")
    if header and header.strip():
        return header.strip()
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def _is_agent_token(offered: str | None) -> bool:
    """Constant-time comparison against the configured machine credential.

    An unset credential never matches, so a deployment that forgot to set one
    cannot be driven by an empty string."""
    configured = settings.operator_agent_token
    if not configured or not offered:
        return False
    return hmac.compare_digest(offered, configured)


# ── Rate limiting ───────────────────────────────────────────────────────
# In-process and per principal. Deliberately simple: this guards a
# single-instance admin surface against a runaway loop, not a public endpoint
# against a botnet. The paid limit is separate and slower, because the cost of
# too many paid jobs is money rather than load.

_requests: dict[str, list[float]] = {}
_paid: dict[str, list[float]] = {}


def _hit(bucket: dict[str, list[float]], key: str, limit: int, window: float) -> bool:
    now = time.monotonic()
    times = [t for t in bucket.get(key, []) if now - t < window]
    if len(times) >= limit:
        bucket[key] = times
        return False
    times.append(now)
    bucket[key] = times
    return True


def check_rate_limit(principal: OperatorPrincipal) -> None:
    if not _hit(_requests, principal.audit_name,
                settings.operator_rate_limit_per_minute, 60.0):
        raise _RATE_LIMITED


def check_paid_rate_limit(principal: OperatorPrincipal) -> None:
    """A separate, slower budget for operations that spend money."""
    if not _hit(_paid, principal.audit_name,
                settings.operator_paid_rate_limit_per_hour, 3600.0):
        raise HTTPException(
            status_code=429,
            detail="Too many paid operator jobs in the last hour",
        )


def reset_rate_limits() -> None:
    """Test hook. Never called by the application."""
    _requests.clear()
    _paid.clear()


async def operator_principal(
    request: Request, db: AsyncSession = Depends(get_db)
) -> OperatorPrincipal:
    """Authenticate an operator request as the agent or as a signed-in admin.

    The machine credential is checked first and, when it matches, no user is
    loaded at all — the agent is not a user, holds no user's data, and must
    not be able to act as one."""
    offered = _offered_token(request)
    if _is_agent_token(offered):
        return OperatorPrincipal(kind="agent", label=settings.operator_agent_label)

    # Not the machine credential: fall back to the ordinary signed-in user.
    # Imported here so this module carries no dependency on the consumer auth
    # path beyond the point of use.
    from app.auth import current_user

    try:
        user: User = await current_user(request, db)
    except HTTPException:
        raise _UNAUTHENTICATED
    return OperatorPrincipal(kind="admin", label=user.email or "admin", user_id=user.id)


async def operator_request(
    principal: OperatorPrincipal = Depends(operator_principal),
) -> OperatorPrincipal:
    """The dependency every operator route uses: authenticate, then rate-limit."""
    check_rate_limit(principal)
    return principal
