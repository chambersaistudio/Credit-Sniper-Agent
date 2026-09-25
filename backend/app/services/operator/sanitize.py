"""
What an operator response is allowed to contain.

The operator surface is authenticated, but it is driven by an agent and read
on a phone, so it is not a place for the consumer's identity or for anything
the provider gave us in confidence. The rule is allow-list where the shape is
known and deny-list where it is not.

Allowed: masked account identifiers, creditor names, extraction state, model
telemetry, benchmark diagnostics, page numbers.

Never: the raw report PDF or any part of it, the consumer's name, address,
date of birth, SSN or phone, storage keys, and any credential.

`safe_error` is the other half of the same rule: a failed job records a class
and a message we wrote, while the provider's own text and the traceback stay
in the server log where only a deployment operator can reach them.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Keys that must never appear in an operator payload, at any depth. Matched
# case-insensitively against the whole key, and by substring for the credential
# families, because a new setting should be excluded by default rather than by
# someone remembering to add it.
_FORBIDDEN_KEYS = {
    "raw_text", "file_data", "document", "pdf", "storage_key",
    "personal_info", "ssn", "ssn_last_four", "date_of_birth", "dob",
    "address", "city", "state", "zip_code", "phone", "full_name", "email",
    "consumer_name", "contact",
}
_FORBIDDEN_SUBSTRINGS = ("api_key", "secret", "password", "token", "credential",
                         "authorization", "private_key")

# Values that look like a credential or a document regardless of their key.
_PDF_MAGIC = "%PDF-"
_DATA_URI = re.compile(r"^data:[^;]+;base64,", re.I)
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_LONG_SECRET = re.compile(r"\b(sk|rk|pk)[-_][A-Za-z0-9_\-]{16,}\b")

REDACTED = "[redacted]"


def _forbidden(key: str) -> bool:
    lowered = key.lower()
    if lowered in _FORBIDDEN_KEYS:
        return True
    return any(part in lowered for part in _FORBIDDEN_SUBSTRINGS)


def _clean_text(value: str) -> str:
    if value.startswith(_PDF_MAGIC) or _DATA_URI.match(value):
        return REDACTED
    value = _SSN.sub(REDACTED, value)
    return _LONG_SECRET.sub(REDACTED, value)


def sanitize(value: Any, *, _depth: int = 0) -> Any:
    """Recursively strip anything an operator response may not carry.

    Applied to every operator payload on the way out, including job results
    read back from the database, so a result stored before a rule existed is
    still filtered when it is served."""
    if _depth > 24:  # pathological nesting is not a shape we produce
        return REDACTED
    if isinstance(value, dict):
        return {
            key: (REDACTED if _forbidden(str(key)) else sanitize(item, _depth=_depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item, _depth=_depth + 1) for item in value]
    if isinstance(value, bytes):
        # Bytes are never a thing an operator needs; the only bytes in reach
        # are document content.
        return REDACTED
    if isinstance(value, str):
        return _clean_text(value)
    return value


# ── Safe errors ─────────────────────────────────────────────────────────

# Failure classes an operator is allowed to see, mapped to wording that says
# what happened without naming a vendor, a status code or a token budget.
_SAFE_MESSAGES = {
    "AIProviderError": "The AI provider was unavailable. Nothing was read and nothing was billed.",
    "AIResponseError": "The model answered unusably (it hit its output budget or failed validation). This was billed.",
    "AIRefusalError": "The model declined the request. This was billed.",
    "AIConfigurationError": "AI access is not configured on this deployment.",
    "NoBankedIndex": "This report has no banked Stage-1 index; run the index pass first.",
    "LookupError": "The requested report, batch or record does not exist.",
    "ValueError": "The request was not valid for this operation.",
    "PageSelectionError": "The requested pages could not be assembled from the stored document.",
    "WorkerInterrupted": "The worker was interrupted while this job was running. A paid job is not "
                         "retried automatically; resubmit with a new idempotency key if you want it re-run.",
}
_GENERIC = "The operation failed. See the server log for details."


def safe_error(error: BaseException | str) -> tuple[str, str]:
    """(class, message) for an operator, from an exception or a class name.

    An unrecognised failure gets the generic message on purpose: a message we
    have not vetted is exactly the one likely to carry a provider's wording,
    a file path or a fragment of the request."""
    name = type(error).__name__ if isinstance(error, BaseException) else str(error)
    return name, _SAFE_MESSAGES.get(name, _GENERIC)
