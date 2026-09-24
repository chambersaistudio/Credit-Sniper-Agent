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
    FAILED = "failed"

    @property
    def allows_evaluation(self) -> bool:
        return self is ExtractionStatus.VERIFIED
