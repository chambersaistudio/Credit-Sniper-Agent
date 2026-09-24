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
    storage_key = Column(String)  # original PDF in document storage (app/services/storage.py)
    raw_text = Column(Text)
    parsed_data = Column(JSON)  # parse metadata (extraction method, personal info), not a copy of the text
    credit_score = Column(Integer)
    score_type = Column(String)  # e.g. "FICO Score 8", as labelled in the report
    # Explicit extraction quality (app/services/document_extraction/status.py).
    # Only "verified" permits dispute-eligibility analysis.
    extraction_status = Column(String, default="extraction_incomplete", nullable=False)
    extraction_audit = Column(JSON)  # second-pass audit findings and reconciliation reasons
    public_records = Column(JSON)
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
    creditor_name = Column(String)  # the company REPORTING this tradeline (furnisher or collector)
    # The debt's origin, and any company it was sold to — separate parties
    # from the furnisher above, and separately disputable.
    original_creditor = Column(String)
    sold_to = Column(String)
    account_number = Column(String)  # as masked on the report
    account_type = Column(String)
    account_status = Column(String)  # normalized: open | closed | paid | charged_off | collection | ...
    account_status_raw = Column(String)  # status exactly as worded in the report
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
    date_status_updated = Column(String)
    terms = Column(String)
    responsibility = Column(String)
    consumer_dispute = Column(String)  # consumer dispute notation, when the report shows one
    contact = Column(JSON)  # furnisher/collector contact details as printed
    payment_history = Column(JSON)  # [{year, month, raw_code, code}] — the month-by-month grid
    remarks = Column(Text)
    # Provenance: which pages this record came from, and short excerpts
    # showing why each important value exists. Feeds the evidence graph.
    source_pages = Column(JSON)
    field_evidence = Column(JSON)
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
