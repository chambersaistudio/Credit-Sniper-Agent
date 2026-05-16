"""
Credit report upload, parsing, and analysis endpoints.
"""
import os
import uuid
import logging
from typing import Any
from datetime import datetime, timezone

import aiofiles
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.config import settings
from app.models.credit_report import CreditReport, CreditAccount, CreditInquiry
from app.services.pdf_parser import parse_credit_report_pdf
from app.services.analysis_engine import analyze_credit_report

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/reports", tags=["reports"])

ALLOWED_CONTENT_TYPES = {"application/pdf", "application/octet-stream"}
MAX_SIZE_BYTES = settings.max_file_size_mb * 1024 * 1024


class ReportResponse(BaseModel):
    id: str
    bureau: str
    credit_score: int | None
    report_date: str | None
    total_accounts: int
    disputable_accounts: int
    total_inquiries: int
    disputable_inquiries: int
    estimated_score_gain: int
    created_at: str

    class Config:
        from_attributes = True


@router.post("/upload", response_model=dict[str, Any])
async def upload_credit_report(
    file: UploadFile = File(...),
    bureau: str = Form(default="auto_detect"),
    user_id: str = Form(default="default"),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload a credit report PDF for parsing and AI analysis.
    Automatically detects bureau, extracts all accounts, and runs dispute analysis.
    """
    if file.content_type not in ALLOWED_CONTENT_TYPES and not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    content = await file.read()
    if len(content) > MAX_SIZE_BYTES:
        raise HTTPException(status_code=400, detail=f"File exceeds {settings.max_file_size_mb}MB limit")

    os.makedirs(settings.upload_dir, exist_ok=True)
    file_id = str(uuid.uuid4())
    file_path = os.path.join(settings.upload_dir, f"{file_id}.pdf")

    async with aiofiles.open(file_path, "wb") as f:
        await f.write(content)

    try:
        parsed = parse_credit_report_pdf(file_path)
    except Exception as e:
        logger.error(f"PDF parse failed: {e}")
        os.unlink(file_path)
        raise HTTPException(status_code=422, detail=f"Could not parse PDF: {str(e)}")

    detected_bureau = parsed.get("bureau", bureau)
    if bureau != "auto_detect":
        detected_bureau = bureau

    report = CreditReport(
        user_id=uuid.UUID(user_id) if user_id != "default" else uuid.uuid4(),
        bureau=detected_bureau,
        credit_score=parsed.get("credit_score"),
        report_date=_parse_date(parsed.get("report_date")),
        source="manual_upload",
        file_path=file_path,
        raw_text=parsed.get("raw_text", "")[:50000],
        parsed_data=parsed,
    )
    db.add(report)
    await db.flush()

    # Run AI analysis
    try:
        analysis = analyze_credit_report(parsed)
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        analysis = {"error": str(e), "disputable_accounts": [], "disputable_inquiries": [], "summary": {}}

    # Store analyzed accounts
    disputable_count = 0
    for item in analysis.get("disputable_accounts", []):
        account = CreditAccount(
            report_id=report.id,
            bureau=detected_bureau,
            creditor_name=item.get("creditor_name"),
            account_number=item.get("account_number"),
            is_disputable=True,
            dispute_reasons=item.get("dispute_reasons", []),
            metro2_violations=item.get("violations", []),
            fcra_violations=[v for v in item.get("violations", []) if v.get("violation_type") == "fcra"],
            priority_score=item.get("priority_score", 5),
            raw_data=item,
        )
        db.add(account)
        disputable_count += 1

    for item in analysis.get("non_disputable_accounts", []):
        account = CreditAccount(
            report_id=report.id,
            bureau=detected_bureau,
            creditor_name=item.get("creditor_name"),
            is_disputable=False,
            raw_data=item,
        )
        db.add(account)

    # Store inquiries
    disputable_inquiry_count = 0
    for inq in analysis.get("disputable_inquiries", []):
        inquiry = CreditInquiry(
            report_id=report.id,
            bureau=detected_bureau,
            creditor_name=inq.get("creditor_name"),
            inquiry_date=inq.get("inquiry_date"),
            inquiry_type="hard",
            is_disputable=True,
            dispute_reason=inq.get("dispute_reason"),
        )
        db.add(inquiry)
        disputable_inquiry_count += 1

    await db.commit()

    summary = analysis.get("summary", {})
    return {
        "report_id": str(report.id),
        "bureau": detected_bureau,
        "credit_score": parsed.get("credit_score"),
        "report_date": parsed.get("report_date"),
        "total_accounts": summary.get("total_accounts", 0),
        "disputable_accounts": disputable_count,
        "total_inquiries": summary.get("total_inquiries", 0),
        "disputable_inquiries": disputable_inquiry_count,
        "estimated_score_gain": summary.get("estimated_score_gain", 0),
        "overall_strategy": summary.get("overall_strategy", ""),
        "highest_priority_items": summary.get("highest_priority_items", []),
        "analysis": analysis,
    }


@router.get("/{report_id}", response_model=dict[str, Any])
async def get_report(report_id: str, db: AsyncSession = Depends(get_db)):
    """Get a credit report with all accounts and analysis."""
    result = await db.execute(
        select(CreditReport).where(CreditReport.id == uuid.UUID(report_id))
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    accounts_result = await db.execute(
        select(CreditAccount).where(CreditAccount.report_id == report.id)
    )
    accounts = accounts_result.scalars().all()

    inquiries_result = await db.execute(
        select(CreditInquiry).where(CreditInquiry.report_id == report.id)
    )
    inquiries = inquiries_result.scalars().all()

    return {
        "id": str(report.id),
        "bureau": report.bureau,
        "credit_score": report.credit_score,
        "report_date": str(report.report_date) if report.report_date else None,
        "pull_date": str(report.pull_date),
        "source": report.source,
        "accounts": [
            {
                "id": str(a.id),
                "creditor_name": a.creditor_name,
                "account_number": a.account_number,
                "account_type": a.account_type,
                "account_status": a.account_status,
                "balance": a.balance,
                "is_disputable": a.is_disputable,
                "priority_score": a.priority_score,
                "dispute_reasons": a.dispute_reasons,
                "metro2_violations": a.metro2_violations,
                "fcra_violations": a.fcra_violations,
            }
            for a in accounts
        ],
        "inquiries": [
            {
                "id": str(i.id),
                "creditor_name": i.creditor_name,
                "inquiry_date": i.inquiry_date,
                "inquiry_type": i.inquiry_type,
                "is_disputable": i.is_disputable,
                "dispute_reason": i.dispute_reason,
            }
            for i in inquiries
        ],
    }


@router.get("/", response_model=list[dict[str, Any]])
async def list_reports(db: AsyncSession = Depends(get_db)):
    """List all uploaded credit reports."""
    result = await db.execute(select(CreditReport).order_by(CreditReport.created_at.desc()))
    reports = result.scalars().all()
    return [
        {
            "id": str(r.id),
            "bureau": r.bureau,
            "credit_score": r.credit_score,
            "pull_date": str(r.pull_date),
            "source": r.source,
        }
        for r in reports
    ]


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        from dateutil import parser
        return parser.parse(date_str).replace(tzinfo=timezone.utc)
    except Exception:
        return None
