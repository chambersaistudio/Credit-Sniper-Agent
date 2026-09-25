"""
The AI layer's own error vocabulary.

Callers never see a vendor SDK's exception types. What they do see is which
KIND of failure happened, because the four kinds have completely different
consequences:

  AIProviderError       transient and free — the call never ran. Retry later.
  AIConfigurationError  our deployment is wrong. Retrying changes nothing.
  AIResponseError       the model ran, we were BILLED, and the answer was
                        unusable (truncated at the token budget, or it failed
                        schema validation). Retrying costs the same money for
                        the same result — this needs a design change.
  AIRefusalError        the model declined. Retrying changes nothing.

Collapsing these loses the only distinction that matters at the moment of
failure: whether trying again is free, pointless, or expensive.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderUsage:
    """What a call actually consumed, even when it failed.

    A response truncated at the token budget is billed in full. Without this
    the usage record for a failed call reads as zero tokens and no cost, which
    understates spend precisely where spend is worst."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: float = 0.0
    # Provider-side handle for a support ticket. Present even with
    # store=false, where the response body itself is not retained.
    response_id: str | None = None
    # For a truncation: what the budget was and what the model spent getting
    # nowhere. Reasoning tokens count against the same budget as the answer.
    max_tokens: int | None = None
    reasoning_tokens: int | None = None


class AIError(Exception):
    """Base for every failure surfaced by the AI layer. Callers catch this,
    never a provider SDK's own exception types.

    `usage` is populated whenever the provider billed us despite failing, so
    the cost of a failure is recorded rather than lost."""

    def __init__(self, *args, usage: ProviderUsage | None = None):
        super().__init__(*args)
        self.usage = usage


class AIConfigurationError(AIError):
    """Unknown provider, missing SDK, or no credentials. Our problem to fix;
    no amount of retrying helps, and it costs nothing."""


class AIProviderError(AIError):
    """The provider API call itself failed (network, rate limit, 5xx, auth).
    Nothing was read and nothing was billed; a later retry is free."""


class AIRefusalError(AIError):
    """The model (and any server-side fallback) declined the request."""


class AIResponseError(AIError):
    """The model answered, but not usably: truncated at the token budget, or
    the output didn't validate against the requested schema.

    This one was PAID FOR. Treating it as a transient outage invites a retry
    that burns the same tokens for the same failure."""
