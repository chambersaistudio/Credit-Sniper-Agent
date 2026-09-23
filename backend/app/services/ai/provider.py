"""
Provider-agnostic AI abstraction with model-tier routing.

Nothing outside this package should import `anthropic` or `openai`
directly, or hardcode a model name — call `complete(tier, ...)` instead.
That keeps the app free to mix providers per task, swap a tier to a
different vendor, or add a new provider without touching call sites.

Tiers (see the Phase 1.5 spec):
  FAST       — cheap/quick: chat, summaries, simple classification/extraction
  REASONING  — dispute eligibility, cross-bureau analysis, strategy
  ESCALATION — ambiguous, high-stakes, or low-confidence cases

The AI's output is never authoritative application state — callers treat
`complete()`'s return as a proposal to validate, not a fact to store.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from app.config import settings

logger = logging.getLogger(__name__)


class ModelTier(str, Enum):
    FAST = "fast"
    REASONING = "reasoning"
    ESCALATION = "escalation"


@dataclass
class CompletionResult:
    text: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float


class AIProvider(Protocol):
    name: str

    def complete(self, *, system: str, prompt: str, model: str, max_tokens: int) -> CompletionResult: ...


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str):
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)

    def complete(self, *, system: str, prompt: str, model: str, max_tokens: int) -> CompletionResult:
        start = time.monotonic()
        response = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return CompletionResult(
            text=response.content[0].text,
            provider=self.name,
            model=model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=(time.monotonic() - start) * 1000,
        )


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str):
        import openai

        self._client = openai.OpenAI(api_key=api_key)

    def complete(self, *, system: str, prompt: str, model: str, max_tokens: int) -> CompletionResult:
        start = time.monotonic()
        response = self._client.chat.completions.create(
            model=model,
            max_completion_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )
        usage = response.usage
        return CompletionResult(
            text=response.choices[0].message.content or "",
            provider=self.name,
            model=model,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            latency_ms=(time.monotonic() - start) * 1000,
        )


_PROVIDER_FACTORIES = {
    "anthropic": lambda: AnthropicProvider(settings.anthropic_api_key),
    "openai": lambda: OpenAIProvider(settings.openai_api_key),
}

# Tier -> (provider name, model id) used when no env override is set.
_TIER_DEFAULTS: dict[ModelTier, tuple[str, str]] = {
    ModelTier.FAST: ("anthropic", "claude-haiku-4-5-20251001"),
    ModelTier.REASONING: ("anthropic", "claude-sonnet-5"),
    ModelTier.ESCALATION: ("anthropic", "claude-opus-5-5"),
}

_provider_instances: dict[str, AIProvider] = {}


def _get_provider_instance(provider_name: str) -> AIProvider:
    if provider_name not in _PROVIDER_FACTORIES:
        raise ValueError(f"Unknown AI provider: {provider_name!r}")
    if provider_name not in _provider_instances:
        _provider_instances[provider_name] = _PROVIDER_FACTORIES[provider_name]()
    return _provider_instances[provider_name]


def resolve_tier(tier: ModelTier) -> tuple[AIProvider, str]:
    """Return (provider, model) for a tier, respecting env/settings overrides."""
    default_provider, default_model = _TIER_DEFAULTS[tier]
    override_provider = getattr(settings, f"ai_{tier.value}_provider", "") or ""
    override_model = getattr(settings, f"ai_{tier.value}_model", "") or ""
    provider_name = override_provider or default_provider
    model = override_model or default_model
    return _get_provider_instance(provider_name), model


def complete(tier: ModelTier, *, system: str, prompt: str, max_tokens: int, task: str = "") -> CompletionResult:
    """Run a completion on the given tier and log provider/model/tokens/latency for cost tracking."""
    provider, model = resolve_tier(tier)
    result = provider.complete(system=system, prompt=prompt, model=model, max_tokens=max_tokens)
    logger.info(
        "ai_completion provider=%s model=%s task=%s tokens_in=%d tokens_out=%d latency_ms=%.0f",
        result.provider, result.model, task, result.input_tokens, result.output_tokens, result.latency_ms,
    )
    return result
