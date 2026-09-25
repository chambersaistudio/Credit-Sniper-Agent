"""
Cross-bureau account identity.

A `CreditAccount` (see app/models/credit_report.py) is always scoped to one
bureau's report upload — the same real-world tradeline shows up as three
separate rows when a consumer uploads Equifax, Experian, and TransUnion
reports. `CanonicalAccount` is the cross-bureau identity those rows link
to; `AccountLink` is the (bureau record -> canonical account) edge, with
the confidence and matched fields that produced it so the match is
auditable rather than an opaque guess.
"""
from sqlalchemy import Column, String, DateTime, Float, ForeignKey, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
import uuid

from app.database import Base


class CanonicalAccount(Base):
    __tablename__ = "canonical_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    # Best-known display values, taken from the highest-confidence linked
    # record — not a merge/average of conflicting bureau data. Conflicts
    # themselves are the comparison engine's job, not this model's.
    creditor_name = Column(String, nullable=False)
    account_type = Column(String)
    account_number_last_four = Column(String(4))
    # Set when this account was created despite closely resembling another
    # canonical account: {candidate_canonical_id, confidence, matched_fields}.
    # An ambiguous match is surfaced for review, never merged silently.
    match_review = Column(JSON)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    links = relationship("AccountLink", back_populates="canonical_account", cascade="all, delete-orphan")


class AccountLink(Base):
    __tablename__ = "account_links"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    canonical_account_id = Column(UUID(as_uuid=True), ForeignKey("canonical_accounts.id"), nullable=False)
    credit_account_id = Column(UUID(as_uuid=True), ForeignKey("credit_accounts.id"), nullable=False, unique=True)
    bureau = Column(String, nullable=False)
    # 0.0-1.0. Below the matcher's threshold, a record is left unlinked
    # (its own single-record CanonicalAccount) rather than force-matched.
    confidence = Column(Float, nullable=False)
    matched_fields = Column(JSON)  # e.g. {"creditor_name": 0.95, "account_number_suffix": 1.0, "date_opened": 0.8}
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    canonical_account = relationship("CanonicalAccount", back_populates="links")
    credit_account = relationship("CreditAccount")
