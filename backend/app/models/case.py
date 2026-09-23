"""
Evidence graph.

  Claim     — a reasoning-engine evaluation of one canonical account: either
              a grounded dispute basis or an explicit "no dispute ground".
              Stored either way, so every decision has a recorded "why".
  Evidence  — the specific finding / document a claim rests on.
  Case      — one dispute to ONE recipient (a bureau or a furnisher), since
              each recipient runs its own investigation clock. Bundles one
              or more claims.
  CaseEvent — append-only audit trail of everything that happened to a case.
"""
from datetime import datetime, timezone
import uuid

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, JSON, String, Table, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


case_claims = Table(
    "case_claims",
    Base.metadata,
    Column("case_id", UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), primary_key=True),
    Column("claim_id", UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), primary_key=True),
)


class Claim(Base):
    __tablename__ = "claims"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    canonical_account_id = Column(UUID(as_uuid=True), ForeignKey("canonical_accounts.id"), nullable=False, index=True)

    has_dispute_ground = Column(Boolean, nullable=False)
    # dispute_bureau | dispute_furnisher | dispute_both | no_dispute | need_more_evidence
    recommended_action = Column(String, nullable=False)
    reasoning = Column(Text, nullable=False)
    disputed_fields = Column(JSON, default=list)
    recipients = Column(JSON, default=list)  # e.g. ["equifax", "furnisher"]
    legal_reference_ids = Column(JSON, default=list)  # ids from app/services/legal_references.py
    legal_explanations = Column(JSON, default=dict)  # reference id -> why it applies here
    requested_remedy = Column(Text)
    additional_evidence_needed = Column(JSON, default=list)
    confidence = Column(Float, nullable=False)
    model_tier = Column(String)
    model = Column(String)
    # current | superseded (a newer evaluation of the same account exists)
    status = Column(String, default="current", nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now)

    canonical_account = relationship("CanonicalAccount")
    evidence = relationship("Evidence", back_populates="claim", cascade="all, delete-orphan")
    cases = relationship("Case", secondary=case_claims, back_populates="claims")


class Evidence(Base):
    __tablename__ = "evidence"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    claim_id = Column(UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), nullable=False, index=True)
    source_type = Column(String, nullable=False)  # finding | report_record | user_document | bureau_response
    source_ref = Column(String)  # finding rule id, credit_account id, or document id
    bureau = Column(String)
    description = Column(Text, nullable=False)
    data = Column(JSON)
    created_at = Column(DateTime(timezone=True), default=_now)

    claim = relationship("Claim", back_populates="evidence")


class Case(Base):
    __tablename__ = "cases"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    canonical_account_id = Column(UUID(as_uuid=True), ForeignKey("canonical_accounts.id"), nullable=False, index=True)
    recipient_type = Column(String, nullable=False)  # bureau | furnisher
    recipient_name = Column(String, nullable=False)  # "equifax", or the furnisher's name
    recipient_address = Column(Text)
    status = Column(String, default="draft", nullable=False, index=True)

    # Dispute package, generated from claims + evidence (see dispute_package.py).
    package = Column(JSON)
    package_generated_at = Column(DateTime(timezone=True))

    submission_channel = Column(String)  # manual_mail | certified_mail | portal | email | api
    tracking_number = Column(String)
    submitted_at = Column(DateTime(timezone=True))
    delivered_at = Column(DateTime(timezone=True))
    response_due_at = Column(DateTime(timezone=True))
    deadline_basis = Column(String)  # delivered | submitted_estimate
    extended_investigation = Column(Boolean, default=False, nullable=False)
    response_received_at = Column(DateTime(timezone=True))
    outcome = Column(String)

    created_at = Column(DateTime(timezone=True), default=_now)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    canonical_account = relationship("CanonicalAccount")
    claims = relationship("Claim", secondary=case_claims, back_populates="cases")
    events = relationship("CaseEvent", back_populates="case", cascade="all, delete-orphan", order_by="CaseEvent.created_at")


class CaseEvent(Base):
    __tablename__ = "case_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id = Column(UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String, nullable=False)  # created | status_change | package_generated | note | ...
    from_status = Column(String)
    to_status = Column(String)
    actor = Column(String, default="user", nullable=False)  # user | system | agent
    detail = Column(Text)
    data = Column(JSON)
    created_at = Column(DateTime(timezone=True), default=_now)

    case = relationship("Case", back_populates="events")
