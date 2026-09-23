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
from app.services.account_matcher import (
    match_account_to_existing,
    normalize_creditor_name,
    account_number_suffix,
)
from app.utils.default_user import get_or_create_default_user

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

    if user_id != "default":
        resolved_user_id = uuid.UUID(user_id)
    else:
        resolved_user_id = (await get_or_create_default_user(db)).id

    report = CreditReport(
        user_id=resolved_user_id,
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

    # AI analysis is a proposal that ANNOTATES the deterministically-parsed
    # accounts below (is_disputable, violations, priority) — it never
    # creates account records itself. If it fails, accounts still get
    # stored with their real parsed fields; they just won't have
    # disputability findings until analysis succeeds. This is what keeps
    # "do not silently hallucinate missing fields" true even when the AI
    # call errors out entirely.
    try:
        analysis = analyze_credit_report(parsed)
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        analysis = {
            "error": str(e),
            "disputable_accounts": [],
            "non_disputable_accounts": [],
            "disputable_inquiries": [],
            "summary": {},
        }

    raw_accounts = parsed.get("accounts_raw", [])
    parser_found_accounts = not (
        len(raw_accounts) == 1 and raw_accounts[0].get("extraction_method") == "full_text_ai_parse"
    )

    ai_disputable = analysis.get("disputable_accounts", [])
    ai_non_disputable = analysis.get("non_disputable_accounts", [])

    disputable_count = 0
    if parser_found_accounts:
        # Ground truth: one CreditAccount per block the regex parser
        # actually found in the document, populated from its own fields.
        for raw in raw_accounts:
            finding, is_dispute = _match_ai_finding(raw, ai_disputable, ai_non_disputable)
            violations = finding.get("violations", []) if finding else []
            account = CreditAccount(
                report_id=report.id,
                bureau=detected_bureau,
                creditor_name=raw.get("creditor_name"),
                account_number=raw.get("account_number"),
                account_type=raw.get("account_type"),
                account_status=raw.get("account_status"),
                balance=raw.get("balance"),
                credit_limit=raw.get("credit_limit"),
                date_opened=raw.get("date_opened"),
                date_closed=raw.get("date_closed"),
                date_of_first_delinquency=raw.get("date_of_first_delinquency"),
                date_last_reported=raw.get("date_last_reported"),
                is_disputable=is_dispute,
                dispute_reasons=finding.get("dispute_reasons", []) if finding else [],
                metro2_violations=violations,
                fcra_violations=[v for v in violations if v.get("violation_type") == "fcra"],
                priority_score=finding.get("priority_score", 0) if finding else 0,
                raw_data={"parsed": raw, "ai_finding": finding},
            )
            db.add(account)
            await db.flush()
            await match_account_to_existing(db, resolved_user_id, account)
            if is_dispute:
                disputable_count += 1
    else:
        # The deterministic parser couldn't segment this document into
        # per-account blocks at all — fall back to the AI's own account
        # list as the only available source. Clearly marked as such in
        # raw_data rather than presented as independently verified.
        for item in ai_disputable:
            account = _build_account_from_ai_item(report.id, detected_bureau, item, is_disputable=True)
            db.add(account)
            await db.flush()
            await match_account_to_existing(db, resolved_user_id, account)
            disputable_count += 1
        for item in ai_non_disputable:
            account = _build_account_from_ai_item(report.id, detected_bureau, item, is_disputable=False)
            db.add(account)
            await db.flush()
            await match_account_to_existing(db, resolved_user_id, account)

    # Inquiries: same principle — the parser's raw_inquiries are ground
    # truth, AI analysis only flags which ones lack permissible purpose.
    ai_disputable_inquiries = analysis.get("disputable_inquiries", [])
    disputable_inquiry_count = 0
    for raw_inq in parsed.get("inquiries_raw", []):
        ai_match = _match_ai_inquiry(raw_inq, ai_disputable_inquiries)
        inquiry = CreditInquiry(
            report_id=report.id,
            bureau=detected_bureau,
            creditor_name=raw_inq.get("creditor_name"),
            inquiry_date=raw_inq.get("inquiry_date"),
            inquiry_type=raw_inq.get("inquiry_type", "hard"),
            is_disputable=bool(ai_match),
            dispute_reason=ai_match.get("dispute_reason") if ai_match else None,
        )
        db.add(inquiry)
        if ai_match:
            disputable_inquiry_count += 1

    await db.commit()

    summary = analysis.get("summary", {})
    total_accounts = len(raw_accounts) if parser_found_accounts else len(ai_disputable) + len(ai_non_disputable)
    return {
        "report_id": str(report.id),
        "bureau": detected_bureau,
        "credit_score": parsed.get("credit_score"),
        "report_date": parsed.get("report_date"),
        "total_accounts": total_accounts,
        "disputable_accounts": disputable_count,
        "total_inquiries": len(parsed.get("inquiries_raw", [])),
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


def _match_ai_finding(
    raw_account: dict[str, Any],
    ai_disputable: list[dict[str, Any]],
    ai_non_disputable: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, bool]:
    """
    Match a parser-extracted account block to the AI's finding for the
    same account, if any. Matched by account number suffix first (most
    reliable), falling back to normalized creditor name. Returns
    (finding_or_None, is_disputable) — an unmatched account is treated as
    not (yet) found disputable, never silently dropped.
    """
    raw_suffix = account_number_suffix(raw_account.get("account_number"))
    raw_name = normalize_creditor_name(raw_account.get("creditor_name"))

    for is_dispute, items in ((True, ai_disputable), (False, ai_non_disputable)):
        for item in items:
            item_suffix = account_number_suffix(item.get("account_number"))
            if raw_suffix and item_suffix:
                if raw_suffix == item_suffix:
                    return item, is_dispute
                continue
            item_name = normalize_creditor_name(item.get("creditor_name"))
            if raw_name and item_name and raw_name == item_name:
                return item, is_dispute
    return None, False


def _match_ai_inquiry(
    raw_inquiry: dict[str, Any], ai_disputable_inquiries: list[dict[str, Any]]
) -> dict[str, Any] | None:
    raw_name = normalize_creditor_name(raw_inquiry.get("creditor_name"))
    for item in ai_disputable_inquiries:
        item_name = normalize_creditor_name(item.get("creditor_name"))
        if raw_name and item_name and raw_name == item_name:
            return item
    return None


def _build_account_from_ai_item(
    report_id: uuid.UUID, bureau: str, item: dict[str, Any], is_disputable: bool
) -> CreditAccount:
    """
    Build a CreditAccount straight from the AI's output — only used when
    the deterministic parser found no per-account structure to match
    against, so there's no ground-truth alternative. Marked in raw_data
    so it's traceable as AI-sourced rather than independently verified.
    """
    violations = item.get("violations", []) if is_disputable else []
    return CreditAccount(
        report_id=report_id,
        bureau=bureau,
        creditor_name=item.get("creditor_name"),
        account_number=item.get("account_number"),
        date_opened=item.get("date_opened"),
        is_disputable=is_disputable,
        dispute_reasons=item.get("dispute_reasons", []) if is_disputable else [],
        metro2_violations=violations,
        fcra_violations=[v for v in violations if v.get("violation_type") == "fcra"],
        priority_score=item.get("priority_score", 0) if is_disputable else 0,
        raw_data={"ai_item": item, "_source": "ai_fallback_parse"},
    )
