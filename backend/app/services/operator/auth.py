"""
Who may drive the operator control plane, and what that permission is worth.

Two principals, one surface:

  agent  a machine credential (OPERATOR_AGENT_TOKEN) held by the review/QA
         agent. It exists so production testing can happen without a shell.
  admin  a signed-in user whose VERIFIED TOKEN is allowlisted — by subject
         (OPERATOR_ADMIN_SUBJECTS) or by a provider-signed email claim
         (OPERATOR_ADMIN_EMAILS) — for the mobile operator page. Signed in is
         not enough, and the consumer's own profile fields never count.

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


def _is_operator_admin(claims: dict) -> bool:
    """Whether this verified token may drive the operator surface.

    Decided on the token's CLAIMS — what the auth provider signed — and never on
    the `User` row. `User.email` is the consumer's own profile field, editable
    through PATCH /api/users/me with no verification, so authorizing on it let
    any signed-in account type the owner's address into its profile and become
    an operator.

    Two ways in, both provider-signed:

    * `sub` on OPERATOR_ADMIN_SUBJECTS — the provider's stable user id. Present
      in every token, immutable, and needs no provider-side configuration. The
      preferred form.
    * an `email` claim on OPERATOR_ADMIN_EMAILS — only when the token carries
      one (Clerk's default session token does not; it needs a custom claim),
      and never when the token says that address is unverified.

    Fail-closed while auth is on: empty lists admit nobody. With auth disabled
    there is one fixed local user and no one to keep out — that mode is already
    documented as dev/test only.
    """
    if not settings.auth_enabled:
        return True
    subject = str(claims.get("sub") or "").strip()
    if subject and subject in settings.operator_admin_subject_set:
        return True
    email = claims.get("email")
    if isinstance(email, str) and email.strip() and claims.get("email_verified") is not False:
        return email.strip().lower() in settings.operator_admins
    return False


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
    from app.auth import authenticate

    try:
        user, claims = await authenticate(request, db)
    except HTTPException:
        raise _UNAUTHENTICATED
    if not _is_operator_admin(claims):
        # Signed in is not the same as operator: this surface spends money and
        # reads every report's telemetry, not only the caller's own. Same
        # wording as an unauthenticated request, so the response cannot be used
        # to enumerate who is on the list.
        #
        # The log names the auth subject — an opaque provider id, the value to
        # put on OPERATOR_ADMIN_SUBJECTS — and whether the token carried an
        # email claim at all, which is the usual reason an email allowlist
        # "does not work". It never logs the email itself.
        logger.warning(
            "Operator access refused for user %s: auth subject %s is not on "
            "OPERATOR_ADMIN_SUBJECTS, and the token %s",
            user.id, claims.get("sub"),
            "carries an email claim that is not on OPERATOR_ADMIN_EMAILS"
            if claims.get("email") else "carries no email claim",
        )
        raise _UNAUTHENTICATED
    return OperatorPrincipal(kind="admin", label=str(claims.get("sub") or "admin"),
                             user_id=user.id)


async def operator_request(
    principal: OperatorPrincipal = Depends(operator_principal),
) -> OperatorPrincipal:
    """The dependency every operator route uses: authenticate, then rate-limit."""
    check_rate_limit(principal)
    return principal
