"""
Durable operator jobs.

The operator control plane exists so that production QA can be driven from a
phone or by an agent over HTTPS, without a shell on the box. Each request
becomes a row here, and a worker inside the Railway backend — where the
database, R2 and provider credentials already live — executes it against the
services we already built.

The row is the unit of durability and the unit of accounting. It records what
was asked, who asked, what it was allowed to spend, what it actually cost and
a sanitized result. Nothing about a job is held only in process memory, so a
restart never loses one and never silently repeats a paid one.
"""
from datetime import datetime, timezone
import uuid

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.database import Base


class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


TERMINAL = (JobStatus.SUCCEEDED, JobStatus.FAILED)


class OperatorJob(Base):
    """One allowlisted operator operation, queued and executed durably."""

    __tablename__ = "operator_jobs"
    __table_args__ = (
        # Per principal, not global: two operators may each use "b0-luna"
        # without one silently receiving the other's job.
        UniqueConstraint("requested_by", "idempotency_key",
                         name="uq_operator_jobs_principal_idempotency"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # The allowlisted operation name. Never a command, never code.
    operation = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default=JobStatus.QUEUED, index=True)
    report_id = Column(UUID(as_uuid=True), index=True)

    # Who asked. Either "agent:<label>" for the machine credential or
    # "user:<uuid>" for a signed-in admin. Never a token or any part of one.
    requested_by = Column(String, nullable=False)
    request_json = Column(JSONB)
    result_json = Column(JSONB)

    # Failures are recorded in the operator's vocabulary, not the provider's:
    # an exception class we chose and a message we wrote. Raw provider text
    # and stack traces stay in the server log.
    safe_error_class = Column(String)
    safe_error_message = Column(Text)

    estimated_cost_usd = Column(Float)
    # What the operation was permitted to spend before it ran, so an audit
    # can show the budget was set in advance rather than inferred after.
    max_model_calls = Column(Integer, nullable=False, default=0)
    model_calls_made = Column(Integer, nullable=False, default=0)
    # A paid job interrupted mid-flight is failed rather than retried, so
    # this records how many times it was picked up at all.
    attempt_count = Column(Integer, nullable=False, default=0)

    # Unique per principal. A repeat of the same key with the same request is
    # answered with the original job instead of buying the work twice.
    idempotency_key = Column(String, index=True)
    request_fingerprint = Column(String)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL
