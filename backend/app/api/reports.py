"""
Credit report ingestion. Upload stores exactly what the document says —
parsed deterministically, or via a source-verified AI extraction fallback
when the layout defeats the parser — then links each tradeline to its
cross-bureau canonical account. No dispute judgment happens here.
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import current_user, current_user_id
from app.config import settings
from app.database import get_db
from app.models.credit_report import CreditAccount, CreditInquiry, CreditReport
from app.models.user import User
from app.services.account_matcher import link_accounts
from app.services.ai import AIError
from app.services.ai_extraction import extract_with_ai
from app.services.pdf_parser import parse_credit_report_pdf
from app.services.redaction import Identity
from app.services.storage import get_storage, report_key
from app.utils.dates import parse_report_date
from app.utils.default_user import parse_uuid

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/reports", tags=["reports"])

SUPPORTED_BUREAUS = {"equifax", "experian", "transunion"}
MAX_SIZE_BYTES = settings.max_file_size_mb * 1024 * 1024
PDF_MAGIC = b"%PDF-"

_ACCOUNT_COLUMNS = (
    "creditor_name", "account_number", "account_type", "account_status", "payment_status",
    "balance", "past_due_amount", "high_balance", "credit_limit", "original_amount", "monthly_payment",
    "date_opened", "date_closed", "date_of_first_delinquency", "date_last_reported",
    "date_last_payment", "date_last_active", "remarks",
)
# Parser fields kept only in raw_data for traceability (no dedicated column).
_RAW_ONLY = ("raw_block", "account_status_raw", "original_creditor", "extraction_method")


def _choose_bureau(requested: str, detected: str) -> str:
    bureau = detected if requested == "auto_detect" else requested
    if bureau == "tri_merge":
        raise HTTPException(
            status_code=422,
            detail="Tri-merge reports aren't supported yet. Upload each bureau's report separately.",
        )
    if bureau not in SUPPORTED_BUREAUS:
        raise HTTPException(
            status_code=422,
            detail="Couldn't tell which bureau this report is from. Choose Equifax, Experian, or TransUnion.",
        )
    return bureau


def _build_account(report_id: uuid.UUID, bureau: str, raw: dict[str, Any]) -> CreditAccount:
    return CreditAccount(
        report_id=report_id,
        bureau=bureau,
        raw_data={key: raw[key] for key in _RAW_ONLY if key in raw},
        **{column: raw.get(column) for column in _ACCOUNT_COLUMNS},
    )


@router.post("/upload", response_model=dict[str, Any])
async def upload_credit_report(
    file: UploadFile = File(...),
    bureau: str = Form(default="auto_detect"),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    content = await file.read()
    if not content.startswith(PDF_MAGIC):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")
    if len(content) > MAX_SIZE_BYTES:
        raise HTTPException(status_code=400, detail=f"File exceeds {settings.max_file_size_mb}MB limit")

    resolved_user_id = user.id

    try:
        parsed = await run_in_threadpool(parse_credit_report_pdf, content)
    except Exception as e:
        logger.exception("PDF parse failed")
        raise HTTPException(status_code=422, detail=f"Could not read this PDF: {e}")
    chosen_bureau = _choose_bureau(bureau, parsed.get("bureau", "unknown"))

    raw_text: str = parsed.get("raw_text", "")
    if not raw_text.strip():
        raise HTTPException(
            status_code=422,
            detail="This PDF has no readable text (it may be a scanned image). Download the report as a text PDF.",
        )

    raw_accounts = parsed.get("accounts_raw", [])
    raw_inquiries = parsed.get("inquiries_raw", [])
    extraction_method = "parser"
    ungrounded_values_dropped = 0
    redactions: dict[str, int] = {}

    parser_failed = len(raw_accounts) == 1 and raw_accounts[0].get("extraction_method") == "full_text_ai_parse"
    if parser_failed:
        raw_accounts = []
        identity = Identity.from_sources(parsed.get("personal_info"), user)
        try:
            extraction = await extract_with_ai(raw_text, identity, context={"user_id": str(resolved_user_id)})
            raw_accounts = extraction.accounts
            raw_inquiries = raw_inquiries or extraction.inquiries
            ungrounded_values_dropped = extraction.ungrounded_values_dropped
            redactions = extraction.redactions
            extraction_method = "ai_verified"
        except AIError as e:
            logger.warning("AI extraction fallback failed: %s", e)
            extraction_method = "failed"

    report = CreditReport(
        user_id=resolved_user_id,
        bureau=chosen_bureau,
        credit_score=parsed.get("credit_score"),
        report_date=_to_datetime(parsed.get("report_date")),
        source="manual_upload",
        raw_text=raw_text,
        parsed_data={
            "personal_info": parsed.get("personal_info", {}),
            "pages": parsed.get("pages"),
            "extraction_method": extraction_method,
            "ungrounded_values_dropped": ungrounded_values_dropped,
            "redactions_before_ai": redactions,
        },
    )
    db.add(report)
    await db.flush()
    report.storage_key = report_key(resolved_user_id, report.id)
    try:
        await get_storage().put(report.storage_key, content, "application/pdf")
    except Exception:
        logger.exception("Storing report PDF failed")
        raise HTTPException(status_code=503, detail="Couldn't store the report file. Try again.")

    accounts = [_build_account(report.id, chosen_bureau, raw) for raw in raw_accounts]
    db.add_all(accounts)
    db.add_all(
        CreditInquiry(
            report_id=report.id,
            bureau=chosen_bureau,
            creditor_name=inq.get("creditor_name"),
            inquiry_date=inq.get("inquiry_date"),
            inquiry_type=inq.get("inquiry_type", "hard"),
        )
        for inq in raw_inquiries
    )
    await db.flush()
    await link_accounts(db, resolved_user_id, accounts)
    await db.commit()

    return {
        "report_id": str(report.id),
        "user_id": str(resolved_user_id),
        "bureau": chosen_bureau,
        "credit_score": report.credit_score,
        "report_date": parsed.get("report_date"),
        "extraction_method": extraction_method,
        "total_accounts": len(accounts),
        "total_inquiries": len(raw_inquiries),
        "warnings": _warnings(extraction_method, ungrounded_values_dropped, len(accounts)),
    }


def _warnings(method: str, dropped: int, account_count: int) -> list[str]:
    warnings = []
    if method == "ai_verified":
        warnings.append(
            "This report's layout wasn't recognized, so accounts were extracted with AI and each value "
            "was checked against the document text. Review the accounts for accuracy."
        )
        if dropped:
            warnings.append(f"{dropped} extracted value(s) didn't appear in the document and were left blank.")
    if method == "failed" or account_count == 0:
        warnings.append("No accounts could be read from this report.")
    return warnings


@router.get("/", response_model=list[dict[str, Any]])
async def list_reports(user_id: uuid.UUID = Depends(current_user_id), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(CreditReport).where(CreditReport.user_id == user_id).order_by(CreditReport.created_at.desc())
    )
    return [_report_summary(r) for r in result.scalars().all()]


async def _owned_report(db: AsyncSession, report_id: str, user_id: uuid.UUID) -> CreditReport:
    """Load a report only if it belongs to the caller. A report owned by
    someone else returns the same 404 as one that doesn't exist, so the
    response never reveals that another user's report exists."""
    result = await db.execute(
        select(CreditReport)
        .where(CreditReport.id == parse_uuid(report_id, "report_id"), CreditReport.user_id == user_id)
        .options(selectinload(CreditReport.accounts), selectinload(CreditReport.inquiries))
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


@router.get("/{report_id}", response_model=dict[str, Any])
async def get_report(
    report_id: str, user_id: uuid.UUID = Depends(current_user_id), db: AsyncSession = Depends(get_db)
):
    report = await _owned_report(db, report_id, user_id)
    return {
        **_report_summary(report),
        "accounts": [
            {"id": str(a.id), **{column: getattr(a, column) for column in _ACCOUNT_COLUMNS}}
            for a in report.accounts
        ],
        "inquiries": [
            {"id": str(i.id), "creditor_name": i.creditor_name, "inquiry_date": i.inquiry_date, "inquiry_type": i.inquiry_type}
            for i in report.inquiries
        ],
    }


@router.get("/{report_id}/file")
async def download_report_file(
    report_id: str, user_id: uuid.UUID = Depends(current_user_id), db: AsyncSession = Depends(get_db)
):
    """Return the stored original PDF — only after verifying the caller owns
    the report. On R2 we hand back a short-lived presigned URL (the browser
    fetches the object directly, and the credentials never leave the server);
    on local disk we stream the bytes through the API. Either way the file is
    private and reachable only through this authorized route."""
    report = await _owned_report(db, report_id, user_id)
    if not report.storage_key:
        raise HTTPException(status_code=404, detail="No stored file for this report")
    storage = get_storage()
    signed = storage.signed_url(report.storage_key)
    if signed is not None:
        return RedirectResponse(url=signed, status_code=307)
    try:
        data = await storage.get(report.storage_key)
    except Exception:
        logger.exception("Reading stored report failed")
        raise HTTPException(status_code=404, detail="No stored file for this report")
    return Response(
        data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="report-{report_id[:8]}.pdf"'},
    )


def _report_summary(report: CreditReport) -> dict[str, Any]:
    return {
        "id": str(report.id),
        "bureau": report.bureau,
        "credit_score": report.credit_score,
        "report_date": report.report_date.date().isoformat() if report.report_date else None,
        "pull_date": report.pull_date.isoformat() if report.pull_date else None,
        "source": report.source,
        "extraction_method": (report.parsed_data or {}).get("extraction_method"),
    }


def _to_datetime(value: str | None) -> datetime | None:
    parsed = parse_report_date(value)
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc) if parsed else None
