from sqlalchemy import Column, String, DateTime, Float, Integer, Text, ForeignKey, Boolean, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
import uuid

from app.database import Base


class CreditReport(Base):
    __tablename__ = "credit_reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    bureau = Column(String, nullable=False)  # equifax | experian | transunion | tri_merge
    report_date = Column(DateTime(timezone=True))
    pull_date = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    source = Column(String, default="manual_upload")  # manual_upload | api_pull
    file_path = Column(String)
    raw_text = Column(Text)
    parsed_data = Column(JSON)
    credit_score = Column(Integer)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="credit_reports")
    accounts = relationship("CreditAccount", back_populates="report", cascade="all, delete-orphan")
    inquiries = relationship("CreditInquiry", back_populates="report", cascade="all, delete-orphan")


class CreditAccount(Base):
    __tablename__ = "credit_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_id = Column(UUID(as_uuid=True), ForeignKey("credit_reports.id"), nullable=False)
    bureau = Column(String)
    creditor_name = Column(String)
    account_number = Column(String)  # partial/masked
    account_type = Column(String)    # revolving | installment | mortgage | collection | etc
    account_status = Column(String)  # open | closed | charged_off | collection | etc
    balance = Column(Float)
    credit_limit = Column(Float)
    original_amount = Column(Float)
    monthly_payment = Column(Float)
    date_opened = Column(String)
    date_closed = Column(String)
    date_last_active = Column(String)
    date_of_first_delinquency = Column(String)
    date_last_reported = Column(String)
    payment_history = Column(JSON)   # month-by-month payment status
    payment_status = Column(String)  # current | 30d | 60d | 90d | 120d | charged_off
    remarks = Column(Text)
    raw_data = Column(JSON)
    # Analysis flags
    is_disputable = Column(Boolean, default=False)
    dispute_reasons = Column(JSON)   # list of identified issues
    metro2_violations = Column(JSON) # specific Metro 2 field violations
    fcra_violations = Column(JSON)   # specific FCRA violations
    priority_score = Column(Integer, default=0)  # 1-10 priority
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    report = relationship("CreditReport", back_populates="accounts")
    disputes = relationship("Dispute", back_populates="account")


class CreditInquiry(Base):
    __tablename__ = "credit_inquiries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_id = Column(UUID(as_uuid=True), ForeignKey("credit_reports.id"), nullable=False)
    bureau = Column(String)
    creditor_name = Column(String)
    inquiry_date = Column(String)
    inquiry_type = Column(String)  # hard | soft
    is_authorized = Column(Boolean)
    is_disputable = Column(Boolean, default=False)
    dispute_reason = Column(String)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    report = relationship("CreditReport", back_populates="inquiries")
