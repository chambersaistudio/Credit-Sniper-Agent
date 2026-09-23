from datetime import datetime, timezone
import uuid

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.database import Base


class AIUsageLog(Base):
    """One row per AI call, success or failure — the basis for cost per
    report, per case, per consumer, and per resolved case."""

    __tablename__ = "ai_usage_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task = Column(String, nullable=False, index=True)
    tier = Column(String, nullable=False)
    provider = Column(String, nullable=False)
    model = Column(String, nullable=False)
    success = Column(Boolean, nullable=False)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cache_read_tokens = Column(Integer, default=0)
    cache_write_tokens = Column(Integer, default=0)
    latency_ms = Column(Float)
    estimated_cost_usd = Column(Float)
    error = Column(Text)
    user_id = Column(UUID(as_uuid=True), index=True)
    case_id = Column(UUID(as_uuid=True), index=True)
    context = Column(JSONB)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
