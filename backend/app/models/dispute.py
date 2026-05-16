from sqlalchemy import Column, String, DateTime, Integer, Text, ForeignKey, Boolean, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
import uuid

from app.database import Base


class Dispute(Base):
    __tablename__ = "disputes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    account_id = Column(UUID(as_uuid=True), ForeignKey("credit_accounts.id"), nullable=False)
    bureau = Column(String, nullable=False)
    furnisher_name = Column(String)
    furnisher_address = Column(String)
    current_round = Column(Integer, default=1)
    status = Column(String, default="pending_approval")
    # pending_approval | approved | submitted | response_received | resolved | escalated | frivolous_flagged
    dispute_type = Column(String)  # bureau_dispute | furnisher_direct | both
    strategy = Column(String)     # metro2_compliance | section_609 | section_611 | fcra_violation | factual_dispute
    priority = Column(Integer, default=5)  # 1-10
    target_removals = Column(JSON)  # what we're asking to be removed/corrected
    next_action_date = Column(DateTime(timezone=True))
    last_response_date = Column(DateTime(timezone=True))
    outcome = Column(String)  # removed | updated | verified | denied | in_progress
    score_impact_estimate = Column(Integer)  # estimated points to gain
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="disputes")
    account = relationship("CreditAccount", back_populates="disputes")
    letters = relationship("DisputeLetter", back_populates="dispute", cascade="all, delete-orphan")
    rounds = relationship("DisputeRound", back_populates="dispute", cascade="all, delete-orphan")


class DisputeLetter(Base):
    __tablename__ = "dispute_letters"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dispute_id = Column(UUID(as_uuid=True), ForeignKey("disputes.id"), nullable=False)
    round_number = Column(Integer, default=1)
    recipient = Column(String)  # bureau name or furnisher name
    recipient_type = Column(String)  # bureau | furnisher
    letter_type = Column(String)    # metro2_compliance | section_609 | section_611 | fcra_violation | furnisher_direct
    subject = Column(String)
    body = Column(Text)
    status = Column(String, default="draft")  # draft | approved | submitted | delivered
    submission_method = Column(String)  # portal | mail | email
    submitted_at = Column(DateTime(timezone=True))
    delivered_at = Column(DateTime(timezone=True))
    certified_mail_tracking = Column(String)
    legal_citations = Column(JSON)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    dispute = relationship("Dispute", back_populates="letters")


class DisputeRound(Base):
    __tablename__ = "dispute_rounds"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dispute_id = Column(UUID(as_uuid=True), ForeignKey("disputes.id"), nullable=False)
    round_number = Column(Integer, nullable=False)
    sent_date = Column(DateTime(timezone=True))
    response_due_date = Column(DateTime(timezone=True))
    response_received_date = Column(DateTime(timezone=True))
    response_type = Column(String)  # removed | updated | verified | denied | no_response
    response_details = Column(Text)
    bureau_response_raw = Column(Text)
    next_strategy = Column(String)  # escalate | new_angle | furnisher_direct | accept | legal_action
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    dispute = relationship("Dispute", back_populates="rounds")
