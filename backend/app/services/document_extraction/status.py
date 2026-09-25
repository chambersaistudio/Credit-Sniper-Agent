"""
Extraction quality as an explicit, persisted state.

Only VERIFIED permits dispute-eligibility analysis. Anything else means the
document was not read well enough to reason about — which is a statement
about our extraction, never about the account being accurate.
"""
from enum import Enum


class ExtractionStatus(str, Enum):
    VERIFIED = "verified"
    NEEDS_AUDIT = "needs_audit"
    EXTRACTION_INCOMPLETE = "extraction_incomplete"
    # The document itself could not be read — this, and only this, justifies
    # telling the consumer their PDF was unreadable.
    FAILED = "failed"
    # We never got to read the document: the AI provider was unavailable
    # (quota exhausted, rate limited, timeout, 5xx, misconfigured). Nothing is
    # known about the report's contents, and the stored original can simply be
    # re-extracted once the service is back.
    PROVIDER_UNAVAILABLE = "provider_unavailable"

    @property
    def allows_evaluation(self) -> bool:
        return self is ExtractionStatus.VERIFIED

    @property
    def is_retryable(self) -> bool:
        """Re-running extraction on the stored original could plausibly work."""
        return self in (ExtractionStatus.PROVIDER_UNAVAILABLE, ExtractionStatus.FAILED)
