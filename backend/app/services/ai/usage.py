"""
AI usage records for cost tracking. Every generate() call — success or
failure — emits one UsageRecord to the registered listeners. The app
registers a DB sink at startup (see app/main.py); tests register none.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass
class UsageRecord:
    task: str
    tier: str
    provider: str
    model: str
    success: bool
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: float = 0.0
    estimated_cost_usd: float | None = None
    error: str | None = None
    # Attribution for cost-per-case / cost-per-consumer rollups.
    context: dict[str, Any] = field(default_factory=dict)


UsageListener = Callable[[UsageRecord], Awaitable[None]]
_listeners: list[UsageListener] = []


def add_usage_listener(listener: UsageListener) -> None:
    _listeners.append(listener)


def clear_usage_listeners() -> None:
    _listeners.clear()


async def emit(record: UsageRecord) -> None:
    logger.info(
        "ai_usage task=%s tier=%s model=%s success=%s in=%d out=%d cache_read=%d cost=%s latency_ms=%.0f",
        record.task, record.tier, record.model, record.success, record.input_tokens,
        record.output_tokens, record.cache_read_tokens, record.estimated_cost_usd, record.latency_ms,
    )
    for listener in _listeners:
        try:
            await listener(record)
        except Exception:
            # Cost tracking must never break the request it's measuring.
            logger.exception("AI usage listener failed")
