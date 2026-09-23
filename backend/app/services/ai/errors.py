class AIError(Exception):
    """Base for every failure surfaced by the AI layer. Callers catch this,
    never a provider SDK's own exception types."""


class AIConfigurationError(AIError):
    """Unknown provider, missing SDK, or no credentials."""


class AIProviderError(AIError):
    """The provider API call itself failed (network, rate limit, 5xx, auth)."""


class AIRefusalError(AIError):
    """The model (and any server-side fallback) declined the request."""


class AIResponseError(AIError):
    """The model answered, but not usably: truncated at max_tokens, or the
    output didn't validate against the requested schema."""
