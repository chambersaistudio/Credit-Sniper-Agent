"""
Confirmed ground truth for one batch of one report.

Benchmark truth is what a human read off the document: the values a model's
extraction is scored against. It holds real account data — masked numbers,
balances, dates, payment grids — so it is stored server-side, entered or
corrected once, and thereafter selected by reference. It is never committed to
Git, never pasted into a chat, and never typed twice on a phone.

`verified` is the important column. Truth drafted from a model's own
extraction is a starting point, not truth: benchmarking against it would
measure agreement with that model rather than correctness. A draft is stored
unverified and the benchmark refuses it until a human has checked it against
the document and said so.
"""
from datetime import datetime, timezone
import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.database import Base


class BenchmarkTruth(Base):
    """One (report, batch, label) truth set."""

    __tablename__ = "benchmark_truth"
    __table_args__ = (
        # One truth per label per batch. "current" is the default label, so the
        # common case is one row per batch that gets corrected in place.
        UniqueConstraint("report_id", "batch_id", "label",
                         name="uq_benchmark_truth_report_batch_label"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_id = Column(UUID(as_uuid=True), ForeignKey("credit_reports.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    batch_id = Column(String, nullable=False)
    label = Column(String, nullable=False, default="current")

    # {"accounts": [ ... ]} — the same shape the scorer takes. Only the fields
    # a truth entry states are scored, so it can start small and grow.
    accounts = Column(JSONB, nullable=False)
    # Stable hash of the accounts, so a benchmark job can prove it ran against
    # the truth it was queued for rather than a later correction.
    fingerprint = Column(String, nullable=False, index=True)

    # False until a human confirms it against the document. A benchmark will
    # not run against unverified truth.
    verified = Column(Boolean, nullable=False, default=False)
    # Where it came from: "operator" (entered/corrected by hand) or
    # "drafted_from_batch" (prefilled from a banked extraction, which is why
    # it starts unverified).
    source = Column(String, nullable=False, default="operator")
    note = Column(Text)

    created_by = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    @property
    def account_count(self) -> int:
        return len((self.accounts or {}).get("accounts") or [])
