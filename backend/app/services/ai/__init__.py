"""
Provider-agnostic AI layer. The rest of the app calls `generate()` with a
workload tier and a Pydantic output type — never a vendor SDK or a model
name. Output is a *proposal*: callers validate it against deterministic
application state before anything is stored or acted on.
"""
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from app.services.ai.config import ModelTier, TierConfig, estimate_cost_usd, resolve_tier
from app.services.ai.errors import (
    AIConfigurationError,
    AIError,
    AIProviderError,
    AIRefusalError,
    AIResponseError,
)
from app.services.ai.providers import get_provider, register_provider
from app.services.ai.usage import UsageRecord, add_usage_listener, clear_usage_listeners, emit

T = TypeVar("T", bound=BaseModel)


@dataclass
class Generation(Generic[T]):
    output: T
    tier: ModelTier
    model: str


async def generate(
    tier: ModelTier,
    *,
    system: str,
    prompt: str,
    output_type: type[T],
    task: str,
    context: dict[str, Any] | None = None,
    max_tokens: int | None = None,
) -> Generation[T]:
    config = resolve_tier(tier)
    record = UsageRecord(
        task=task, tier=tier.value, provider=config.provider, model=config.model,
        success=False, context=context or {},
    )
    try:
        provider = get_provider(config.provider)
        result = await provider.generate(
            config, system=system, prompt=prompt, output_type=output_type,
            max_tokens=max_tokens or config.max_tokens,
        )
    except AIError as e:
        record.error = f"{type(e).__name__}: {e}"
        await emit(record)
        raise

    record.success = True
    record.model = result.model
    record.input_tokens = result.input_tokens
    record.output_tokens = result.output_tokens
    record.cache_read_tokens = result.cache_read_tokens
    record.cache_write_tokens = result.cache_write_tokens
    record.latency_ms = result.latency_ms
    record.estimated_cost_usd = estimate_cost_usd(
        result.model, result.input_tokens, result.output_tokens, result.cache_read_tokens, result.cache_write_tokens
    )
    await emit(record)
    return Generation(output=result.output, tier=tier, model=result.model)


__all__ = [
    "AIConfigurationError", "AIError", "AIProviderError", "AIRefusalError", "AIResponseError",
    "Generation", "ModelTier", "TierConfig", "UsageRecord",
    "add_usage_listener", "clear_usage_listeners", "generate", "register_provider", "resolve_tier",
]
