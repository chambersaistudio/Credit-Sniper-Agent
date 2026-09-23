"""
Bureau-scoped report data, stored exactly as extracted from the document.
No analysis or dispute judgment lives here — findings are computed from
these rows (app/services/credit_profile.py) and disputes live in Cases.
"""
from sqlalchemy import Column, String, DateTime, Float, Integer, Text, ForeignKey, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
import uuid

from app.database import Base


class CreditReport(Base):
    __tablename__ = "credit_reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    bureau = Column(String, nullable=False)  # equifax | experian | transunion
    report_date = Column(DateTime(timezone=True))
    pull_date = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    source = Column(String, default="manual_upload")  # manual_upload | api_pull
    file_path = Column(String)
    raw_text = Column(Text)
    parsed_data = Column(JSON)  # parse metadata (extraction method, personal info), not a copy of the text
    credit_score = Column(Integer)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="credit_reports")
    accounts = relationship("CreditAccount", back_populates="report", cascade="all, delete-orphan")
    inquiries = relationship("CreditInquiry", back_populates="report", cascade="all, delete-orphan")


class CreditAccount(Base):
    """One tradeline as one bureau reported it in one report. Unknown values stay NULL."""

    __tablename__ = "credit_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_id = Column(UUID(as_uuid=True), ForeignKey("credit_reports.id"), nullable=False, index=True)
    bureau = Column(String)
    creditor_name = Column(String)
    account_number = Column(String)  # as masked on the report
    account_type = Column(String)
    account_status = Column(String)  # normalized: open | closed | paid | charged_off | collection | ...
    payment_status = Column(String)  # as worded on the report
    balance = Column(Float)
    past_due_amount = Column(Float)
    high_balance = Column(Float)
    credit_limit = Column(Float)
    original_amount = Column(Float)
    monthly_payment = Column(Float)
    date_opened = Column(String)
    date_closed = Column(String)
    date_last_active = Column(String)
    date_of_first_delinquency = Column(String)
    date_last_reported = Column(String)
    date_last_payment = Column(String)
    payment_history = Column(JSON)
    remarks = Column(Text)
    # Source evidence: the report text block this record was read from.
    raw_data = Column(JSON)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    report = relationship("CreditReport", back_populates="accounts")


class CreditInquiry(Base):
    __tablename__ = "credit_inquiries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_id = Column(UUID(as_uuid=True), ForeignKey("credit_reports.id"), nullable=False, index=True)
    bureau = Column(String)
    creditor_name = Column(String)
    inquiry_date = Column(String)
    inquiry_type = Column(String)  # hard | soft
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    report = relationship("CreditReport", back_populates="inquiries")
