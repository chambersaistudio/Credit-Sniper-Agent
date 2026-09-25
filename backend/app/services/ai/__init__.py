"""
Provider-agnostic AI layer. The rest of the app calls `generate()` with a
workload tier and a Pydantic output type — never a vendor SDK or a model
name. Output is a *proposal*: callers validate it against deterministic
application state before anything is stored or acted on.
"""
from dataclasses import dataclass, replace
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from app.services.ai.config import ModelTier, TierConfig, estimate_cost_usd, resolve_tier
from app.services.ai.errors import (
    AIConfigurationError,
    AIError,
    AIProviderError,
    AIRefusalError,
    AIResponseError,
    ProviderUsage,
)
from app.services.ai.providers import (
    DocumentUnsupported,
    get_document_provider,
    get_provider,
    register_provider,
)
from app.services.ai.usage import (
    UsageRecord, add_usage_listener, clear_usage_listeners, emit, only_usage_listener,
)

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


async def generate_document(
    tier: ModelTier,
    *,
    system: str,
    prompt: str,
    document: bytes,
    filename: str,
    output_type: type[T],
    task: str,
    context: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    detail: str | None = None,
    model: str | None = None,
) -> Generation[T]:
    """Send an ORIGINAL document (PDF bytes) to a document-capable provider.

    The bytes are never logged and never transformed before the model reads
    them — the whole point is that the model sees the real document rather
    than text some local parser derived from it.

    `model` overrides the tier's model for THIS CALL ONLY. A benchmark runs
    inside the same process as the consumer extraction worker, so choosing a
    model by mutating global settings would let a benchmark silently decide
    which model a consumer's upload was read by. The override is an argument
    for exactly that reason."""
    config = resolve_tier(tier)
    if model:
        config = replace(config, model=model)
    record = UsageRecord(
        task=task, tier=tier.value, provider=config.provider, model=config.model,
        success=False, context={**(context or {}), "document_bytes": len(document)},
    )
    try:
        provider = get_document_provider(config.provider)
        result = await provider.generate_document(
            config, system=system, prompt=prompt, document=document, filename=filename,
            output_type=output_type, max_tokens=max_tokens or config.max_tokens,
            detail=detail or "high",
        )
    except AIError as e:
        record.error = f"{type(e).__name__}: {e}"
        # A truncated or unparseable response was still billed. Record what it
        # cost, or the most expensive failures are the ones that look free.
        _apply_usage(record, e.usage)
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


def _apply_usage(record: UsageRecord, usage) -> None:
    """Fold a failed call's billed usage into its usage record."""
    if usage is None:
        return
    record.input_tokens = usage.input_tokens
    record.output_tokens = usage.output_tokens
    record.cache_read_tokens = usage.cache_read_tokens
    record.cache_write_tokens = usage.cache_write_tokens
    record.latency_ms = usage.latency_ms
    record.estimated_cost_usd = estimate_cost_usd(
        record.model, usage.input_tokens, usage.output_tokens,
        usage.cache_read_tokens, usage.cache_write_tokens,
    )
    # Operator-only detail for diagnosing an expensive failure.
    record.context = {
        **record.context,
        **({"response_id": usage.response_id} if usage.response_id else {}),
        **({"max_output_tokens": usage.max_tokens} if usage.max_tokens else {}),
        **({"reasoning_tokens": usage.reasoning_tokens} if usage.reasoning_tokens else {}),
    }


__all__ = [
    "AIConfigurationError", "AIError", "AIProviderError", "AIRefusalError", "AIResponseError",
    "DocumentUnsupported", "Generation", "ModelTier", "ProviderUsage", "TierConfig", "UsageRecord",
    "add_usage_listener", "clear_usage_listeners", "generate", "generate_document",
    "only_usage_listener",
    "get_document_provider", "register_provider", "resolve_tier",
]
