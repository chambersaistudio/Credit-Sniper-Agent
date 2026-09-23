"""Persists AI usage records. Uses its own session so a usage write never
joins (or rolls back with) the request's transaction."""
import uuid

from app.database import async_session_maker
from app.models.ai_usage import AIUsageLog
from app.services.ai import UsageRecord


def _uuid_or_none(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


async def persist_usage(record: UsageRecord) -> None:
    async with async_session_maker() as session:
        session.add(AIUsageLog(
            task=record.task,
            tier=record.tier,
            provider=record.provider,
            model=record.model,
            success=record.success,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            cache_read_tokens=record.cache_read_tokens,
            cache_write_tokens=record.cache_write_tokens,
            latency_ms=record.latency_ms,
            estimated_cost_usd=record.estimated_cost_usd,
            error=record.error,
            user_id=_uuid_or_none(record.context.get("user_id")),
            case_id=_uuid_or_none(record.context.get("case_id")),
            context=record.context,
        ))
        await session.commit()
