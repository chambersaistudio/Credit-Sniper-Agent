"""
AI usage records for cost tracking. Every generate() call — success or
failure — emits one UsageRecord to the registered listeners. The app
registers a DB sink at startup (see app/main.py); tests register none.
"""
import logging
from contextlib import contextmanager
from contextvars import ContextVar
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
_listener_override: ContextVar[tuple[UsageListener, ...] | None] = ContextVar(
    "ai_usage_listener_override", default=None
)


def add_usage_listener(listener: UsageListener) -> None:
    _listeners.append(listener)


def clear_usage_listeners() -> None:
    _listeners.clear()


@contextmanager
def only_usage_listener(listener: UsageListener):
    """Route usage records in THIS async context to `listener` alone.

    This used to clear the process-global listener list. An operator benchmark
    or finalizer could therefore steal a concurrent consumer extraction's
    usage event (and vice versa). ContextVar keeps the temporary meter scoped
    to the job/task while normal application listeners keep working elsewhere."""
    token = _listener_override.set((listener,))
    try:
        yield
    finally:
        _listener_override.reset(token)


async def emit(record: UsageRecord) -> None:
    logger.info(
        "ai_usage task=%s tier=%s model=%s success=%s in=%d out=%d cache_read=%d cost=%s latency_ms=%.0f",
        record.task, record.tier, record.model, record.success, record.input_tokens,
        record.output_tokens, record.cache_read_tokens, record.estimated_cost_usd, record.latency_ms,
    )
    listeners = _listener_override.get()
    if listeners is None:
        listeners = tuple(_listeners)
    for listener in listeners:
        try:
            await listener(record)
        except Exception:
            # Cost tracking must never break the request it's measuring.
            logger.exception("AI usage listener failed")
