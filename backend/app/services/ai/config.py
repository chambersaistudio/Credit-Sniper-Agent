"""
Model-tier routing and pricing.

Call sites pick a *tier* by workload, never a model name:

  FAST       — status explanations, notifications, simple extraction/classification
  REASONING  — dispute eligibility, cross-bureau analysis, evidence evaluation, strategy
  ESCALATION — ambiguous, high-stakes, or low-confidence cases

Each tier resolves to a provider + model + effort, overridable per
environment (AI_<TIER>_PROVIDER / AI_<TIER>_MODEL / AI_<TIER>_EFFORT) so a
tier can be repointed at another model or vendor without a code change.
"""
from dataclasses import dataclass, replace
from enum import Enum

from app.config import settings


class ModelTier(str, Enum):
    FAST = "fast"
    REASONING = "reasoning"
    ESCALATION = "escalation"
    # Document understanding: the model reads the ORIGINAL PDF, not text we
    # extracted for it. Two passes — an extractor and an independent auditor
    # that re-reads the same document.
    DOCUMENT_EXTRACTION = "document_extraction"
    DOCUMENT_AUDIT = "document_audit"


@dataclass(frozen=True)
class TierConfig:
    provider: str
    model: str
    # Provider-specific depth control (Anthropic output_config.effort).
    # None = provider default. Not every model accepts it (Haiku 4.5 doesn't).
    effort: str | None = None
    # Opt into server-side refusal fallbacks where the provider supports them.
    refusal_fallback: bool = False
    max_tokens: int = 16000


# Escalation is the same model at maximum effort rather than a separate
# model: one cache namespace, and the most capable model thinking harder is
# the better "second opinion" than a cascade across model families.
_DEFAULTS: dict[ModelTier, TierConfig] = {
    ModelTier.FAST: TierConfig(provider="anthropic", model="claude-haiku-4-5", max_tokens=4000),
    ModelTier.REASONING: TierConfig(
        provider="anthropic", model="claude-opus-5", effort="high", refusal_fallback=True, max_tokens=16000
    ),
    ModelTier.ESCALATION: TierConfig(
        provider="anthropic", model="claude-opus-5", effort="max", refusal_fallback=True, max_tokens=32000
    ),
    # Vision-capable models that accept a PDF directly. Correctness is worth
    # more than tokens here: a misread report poisons everything downstream.
    # Both are env-overridable (AI_DOCUMENT_EXTRACTION_MODEL / _AUDIT_MODEL) —
    # confirm the exact model id available to your OpenAI account.
    ModelTier.DOCUMENT_EXTRACTION: TierConfig(provider="openai", model="gpt-5", max_tokens=32000),
    ModelTier.DOCUMENT_AUDIT: TierConfig(provider="openai", model="gpt-5", max_tokens=16000),
}


def resolve_tier(tier: ModelTier) -> TierConfig:
    config = _DEFAULTS[tier]
    provider = getattr(settings, f"ai_{tier.value}_provider", "") or config.provider
    model = getattr(settings, f"ai_{tier.value}_model", "") or config.model
    effort = getattr(settings, f"ai_{tier.value}_effort", "") or config.effort
    if provider != config.provider or model != config.model:
        # An override points somewhere the default's provider-specific knobs
        # may not apply; only keep effort/fallback if explicitly configured.
        return replace(
            config,
            provider=provider,
            model=model,
            effort=getattr(settings, f"ai_{tier.value}_effort", "") or None,
            refusal_fallback=False,
        )
    return replace(config, effort=effort)


# USD per 1M tokens: (input, output). Estimates for cost tracking only —
# the provider's invoice is authoritative. Unknown models get no estimate
# rather than a guessed one.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),  # server-side refusal fallback target
    "claude-opus-5-5": (4.00, 20.00),
    "claude-fable-5-1": (10.00, 50.00),
}
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


def estimate_cost_usd(
    model: str, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0, cache_write_tokens: int = 0
) -> float | None:
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        return None
    input_rate, output_rate = pricing
    total = (
        input_tokens * input_rate
        + output_tokens * output_rate
        + cache_read_tokens * input_rate * CACHE_READ_MULTIPLIER
        + cache_write_tokens * input_rate * CACHE_WRITE_MULTIPLIER
    ) / 1_000_000
    return round(total, 6)
