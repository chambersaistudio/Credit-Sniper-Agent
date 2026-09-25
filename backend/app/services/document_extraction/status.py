"""
Extraction quality as an explicit, persisted state.

Only VERIFIED permits dispute-eligibility analysis. Anything else means the
document was not read well enough to reason about — which is a statement
about our extraction, never about the account being accurate.

The failure states are deliberately NOT one bucket. They differ in the only
way that matters when something goes wrong: whether trying again is free,
pointless, or expensive.

    PROVIDER_UNAVAILABLE   nothing ran, nothing was billed  -> retry is free
    MODEL_RESPONSE_FAILED  it ran, we were billed, the answer was unusable
                           -> retrying buys the same failure again
    MODEL_REFUSED          the model declined                -> needs a human
    CONFIGURATION_ERROR    our deployment is wrong           -> needs an operator
    FAILED                 the DOCUMENT could not be read    -> the one case
                           that says anything about the consumer's PDF

Collapsing these into PROVIDER_UNAVAILABLE is what made a token-budget
truncation look like an outage, and invited a retry that re-bought it.
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
    # (quota exhausted, rate limited, timeout, 5xx). Nothing is known about
    # the report's contents, nothing was billed, and the stored original can
    # simply be re-extracted once the service is back.
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    # The model ran and we were billed, but the answer was unusable: it hit
    # the output-token budget, or it failed schema validation, or it returned
    # no parsed output. Re-running the identical request spends the same money
    # for the same result, so this is NOT consumer-retryable — it is a signal
    # that the extraction needs to be made smaller, not tried harder.
    MODEL_RESPONSE_FAILED = "model_response_failed"
    # The model declined the request outright.
    MODEL_REFUSED = "model_refused"
    # Missing credentials, unknown provider, no SDK. Ours to fix.
    CONFIGURATION_ERROR = "configuration_error"

    @property
    def allows_evaluation(self) -> bool:
        return self is ExtractionStatus.VERIFIED

    @property
    def is_retryable(self) -> bool:
        """Would re-running extraction on the stored original plausibly work?

        Only where the previous attempt cost nothing and the next one could
        genuinely differ. A billed failure is excluded on purpose: offering
        "retry" there spends real money to reproduce a known outcome."""
        return self in (ExtractionStatus.PROVIDER_UNAVAILABLE, ExtractionStatus.FAILED)

    @property
    def was_billed(self) -> bool:
        """Did this failure consume provider spend? Drives cost telemetry and
        the wording of what we offer the consumer next."""
        return self in (
            ExtractionStatus.MODEL_RESPONSE_FAILED,
            ExtractionStatus.MODEL_REFUSED,
        )

    @property
    def is_operational(self) -> bool:
        """Our problem, not the consumer's document. These must never be
        reported as a fault in the uploaded PDF."""
        return self in (
            ExtractionStatus.PROVIDER_UNAVAILABLE,
            ExtractionStatus.MODEL_RESPONSE_FAILED,
            ExtractionStatus.MODEL_REFUSED,
            ExtractionStatus.CONFIGURATION_ERROR,
        )

    @classmethod
    def for_error(cls, error: Exception | str | None) -> "ExtractionStatus":
        """Map an AI-layer failure to its extraction state.

        Accepts the exception or the recorded "ClassName: message" string, so
        a stored operator record classifies the same way a live failure does.
        """
        name = type(error).__name__ if isinstance(error, Exception) else str(error or "").split(":", 1)[0].strip()
        return {
            "AIProviderError": cls.PROVIDER_UNAVAILABLE,
            "AIResponseError": cls.MODEL_RESPONSE_FAILED,
            "AIRefusalError": cls.MODEL_REFUSED,
            "AIConfigurationError": cls.CONFIGURATION_ERROR,
            "DocumentUnsupported": cls.CONFIGURATION_ERROR,
        }.get(name, cls.PROVIDER_UNAVAILABLE)


# The short internal reason recorded on the report for each operational
# failure. Kept accurate per class: "AI extraction was unavailable" is simply
# false when the model ran, answered, and billed us for an unusable answer.
OPERATIONAL_REASONS: dict[ExtractionStatus, str] = {
    ExtractionStatus.PROVIDER_UNAVAILABLE:
        "AI extraction was unavailable, so the document was never analyzed.",
    ExtractionStatus.MODEL_RESPONSE_FAILED:
        "The reader could not return a complete result for this document, so it was not analyzed.",
    ExtractionStatus.MODEL_REFUSED:
        "The reader stopped before finishing this document, so it was not analyzed.",
    ExtractionStatus.CONFIGURATION_ERROR:
        "Report reading is not configured on this deployment, so the document was not analyzed.",
}


# What the consumer is told for each operational failure. Uniformly free of
# provider names, status codes and token budgets — but no longer uniformly
# "try again later", because for a billed failure that would be a lie that
# costs money.
OPERATIONAL_MESSAGES: dict[ExtractionStatus, str] = {
    ExtractionStatus.PROVIDER_UNAVAILABLE: (
        "Your report was stored safely, but AI extraction is temporarily unavailable. "
        "No report data was analyzed. Retry extraction once the service is available."
    ),
    ExtractionStatus.MODEL_RESPONSE_FAILED: (
        "Your report was stored safely, but we couldn't finish reading it — this report is "
        "larger than our reader currently handles in one pass. Nothing is wrong with your "
        "document, and retrying won't help yet. We've been alerted and are working on it."
    ),
    ExtractionStatus.MODEL_REFUSED: (
        "Your report was stored safely, but our reader stopped before finishing it. "
        "Nothing is wrong with your document. We've been alerted and are looking into it."
    ),
    ExtractionStatus.CONFIGURATION_ERROR: (
        "Your report was stored safely, but report reading isn't available on this "
        "deployment right now. Nothing is wrong with your document. We've been alerted."
    ),
}
