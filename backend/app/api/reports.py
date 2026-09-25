"""
Credit report ingestion.

Upload is deliberately cheap and short: validate the PDF, store the original
privately, create the report row and enqueue extraction. Reading the document
is expensive and slow — an Experian disclosure can outlast a browser or proxy
connection — so it happens in a durable background job
(app/services/extraction_jobs.py) that checkpoints each paid pass. The client
polls for the result instead of holding a request open.

No dispute judgment happens here.
"""
import logging
import uuid
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
from app.models.credit_report import CreditReport
from app.models.user import User
from app.services.document_extraction import ExtractionStatus
from app.services.extraction_jobs import document_sha256, enqueue, is_in_flight
from app.services.pdf_parser import parse_credit_report_pdf
from app.services.report_ingest import ACCOUNT_COLUMNS, SUPPORTED_BUREAUS, resolve_bureau
from app.services.storage import get_storage, report_key
from app.utils.default_user import parse_uuid

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/reports", tags=["reports"])

MAX_SIZE_BYTES = settings.max_file_size_mb * 1024 * 1024
PDF_MAGIC = b"%PDF-"


def _validate_requested_bureau(bureau: str) -> None:
    if bureau == "tri_merge":
        raise HTTPException(
            status_code=422,
            detail="Tri-merge reports aren't supported yet. Upload each bureau's report separately.",
        )
    if bureau != "auto_detect" and bureau not in SUPPORTED_BUREAUS:
        raise HTTPException(status_code=422, detail="Choose Equifax, Experian, or TransUnion.")


@router.post("/upload", response_model=dict[str, Any], status_code=202)
async def upload_credit_report(
    file: UploadFile = File(...),
    bureau: str = Form(default="auto_detect"),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Accept a report for processing. Returns 202 with a report id; the
    reading itself happens in the background and is polled for."""
    content = await file.read()
    if not content.startswith(PDF_MAGIC):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")
    if len(content) > MAX_SIZE_BYTES:
        raise HTTPException(status_code=400, detail=f"File exceeds {settings.max_file_size_mb}MB limit")
    _validate_requested_bureau(bureau)

    # Idempotency. The same original from the same consumer is the same job:
    # a double-click, an impatient re-submit or a retried request must never
    # start a second billable extraction of identical bytes.
    digest = document_sha256(content)
    existing = (await db.execute(
        select(CreditReport)
        .where(CreditReport.user_id == user.id, CreditReport.document_sha256 == digest)
        .order_by(CreditReport.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if existing is not None:
        # A previous attempt that died at the provider is resumed rather than
        # duplicated — its checkpoints are still on the row.
        if not is_in_flight(existing) and ExtractionStatus(existing.extraction_status).is_retryable:
            enqueue(existing, reset_audit=True)
            await db.commit()
        return _accepted(existing, duplicate=True)

    try:
        parsed = await run_in_threadpool(parse_credit_report_pdf, content)
    except Exception as e:
        logger.exception("PDF parse failed")
        raise HTTPException(status_code=422, detail=f"Could not read this PDF: {e}")

    raw_text: str = parsed.get("raw_text", "")
    if not raw_text.strip():
        raise HTTPException(
            status_code=422,
            detail="This PDF has no readable text (it may be a scanned image). Download the report as a text PDF.",
        )
    # With no document model configured, nothing downstream will ever identify
    # the bureau, so an unidentifiable report is rejected now rather than
    # enqueued to fail. With one configured, the model gets its say first.
    if (not settings.document_extraction_enabled
            and resolve_bureau(bureau, parsed.get("bureau")) is None):
        raise HTTPException(
            status_code=422,
            detail="Couldn't tell which bureau this report is from. Choose Equifax, Experian, or TransUnion.",
        )

    # The original PDF is the authoritative document. It is stored privately
    # before anything reads it, and the worker reads the exact stored bytes
    # back — so what we analyse is provably what we kept, and no locally
    # derived text stands in for the report.
    report = CreditReport(
        user_id=user.id, bureau="unknown", source="manual_upload", raw_text=raw_text,
        extraction_status=ExtractionStatus.EXTRACTION_INCOMPLETE.value,
        document_sha256=digest,
        # The requested bureau is the consumer's instruction to the worker,
        # which runs long after this request is gone.
        parsed_data={"requested_bureau": bureau},
    )
    db.add(report)
    await db.flush()
    report.storage_key = report_key(user.id, report.id)
    try:
        await get_storage().put(report.storage_key, content, "application/pdf")
    except Exception:
        logger.exception("Storing report PDF failed")
        raise HTTPException(status_code=503, detail="Couldn't store the report file. Try again.")

    enqueue(report)
    await db.commit()
    return _accepted(report, duplicate=False)


def _accepted(report: CreditReport, *, duplicate: bool) -> dict[str, Any]:
    return {
        "report_id": str(report.id),
        "processing_stage": report.processing_stage,
        "processing": is_in_flight(report),
        # True when these exact bytes were already accepted: the client should
        # follow the same report rather than expect a new one.
        "duplicate": duplicate,
        "status_url": f"/api/reports/{report.id}/status",
        "message": "Your report was received and is being read. This page updates as it progresses.",
    }


@router.get("/", response_model=list[dict[str, Any]])
async def list_reports(user_id: uuid.UUID = Depends(current_user_id), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(CreditReport).where(CreditReport.user_id == user_id).order_by(CreditReport.created_at.desc())
    )
    return [_report_summary(r) for r in result.scalars().all()]


async def _owned_report(
    db: AsyncSession, report_id: str, user_id: uuid.UUID, *, with_rows: bool = True
) -> CreditReport:
    """Load a report only if it belongs to the caller. A report owned by
    someone else returns the same 404 as one that doesn't exist, so the
    response never reveals that another user's report exists."""
    query = select(CreditReport).where(
        CreditReport.id == parse_uuid(report_id, "report_id"), CreditReport.user_id == user_id
    )
    if with_rows:
        query = query.options(selectinload(CreditReport.accounts), selectinload(CreditReport.inquiries))
    report = (await db.execute(query)).scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


@router.get("/{report_id}/status", response_model=dict[str, Any])
async def report_status(
    report_id: str, user_id: uuid.UUID = Depends(current_user_id), db: AsyncSession = Depends(get_db)
):
    """What the background job is doing, for the client to poll.

    Carries no provider internals: which vendor failed, and how, is an
    operational detail kept in logs and on the row for operators."""
    report = await _owned_report(db, report_id, user_id, with_rows=False)
    return _processing_state(report)


@router.get("/{report_id}", response_model=dict[str, Any])
async def get_report(
    report_id: str, user_id: uuid.UUID = Depends(current_user_id), db: AsyncSession = Depends(get_db)
):
    report = await _owned_report(db, report_id, user_id)
    audit = report.extraction_audit or {}
    return {
        **_report_summary(report),
        # Everything needed to audit the extraction: the values read, where
        # they came from, and what the second pass said about them.
        "accounts": [
            {"id": str(a.id), **{column: getattr(a, column) for column in ACCOUNT_COLUMNS}}
            for a in report.accounts
        ],
        "inquiries": [
            {"id": str(i.id), "creditor_name": i.creditor_name, "inquiry_date": i.inquiry_date,
             "inquiry_type": i.inquiry_type, "inquiry_category": i.inquiry_category,
             "business_type": i.business_type}
            for i in report.inquiries
        ],
        "public_records": report.public_records or [],
        "audit_findings": (audit.get("audit") or {}).get("findings") or [],
        "parser_cross_check": (report.parsed_data or {}).get("parser_cross_check"),
    }


@router.post("/{report_id}/retry-extraction", response_model=dict[str, Any], status_code=202)
async def retry_extraction(
    report_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Re-queue extraction against the original PDF already stored for this
    report, so a provider outage doesn't make the consumer upload the same
    file again.

    Like the first attempt this returns immediately: the work is a background
    job, and any pass that already succeeded is reused rather than paid for
    twice.

    Only offered while the report holds no extracted accounts — that is the
    outage case. Once accounts exist they may already carry evaluations and
    cases, and replacing them is a different (destructive) operation."""
    report = await _owned_report(db, report_id, user.id)
    if is_in_flight(report):
        # Already queued or running. Clicking again costs nothing.
        return _accepted(report, duplicate=True)
    if not ExtractionStatus(report.extraction_status).is_retryable:
        raise HTTPException(status_code=409, detail="This report doesn't need re-extraction.")
    if report.accounts:
        raise HTTPException(
            status_code=409,
            detail="This report already has extracted accounts. Upload the report again to replace them.",
        )
    if not report.storage_key:
        raise HTTPException(status_code=409, detail="The original file for this report is no longer available.")

    enqueue(report, reset_audit=True)
    await db.commit()
    return _accepted(report, duplicate=False)


@router.get("/{report_id}/file")
async def download_report_file(
    report_id: str, user_id: uuid.UUID = Depends(current_user_id), db: AsyncSession = Depends(get_db)
):
    """Return the stored original PDF — only after verifying the caller owns
    the report. On R2 we hand back a short-lived presigned URL (the browser
    fetches the object directly, and the credentials never leave the server);
    on local disk we stream the bytes through the API. Either way the file is
    private and reachable only through this authorized route."""
    report = await _owned_report(db, report_id, user_id, with_rows=False)
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


# Consumer-facing wording for each background stage. Never names a provider
# or a vendor error: those are ours to deal with.
_STAGE_MESSAGES = {
    "queued": "Waiting to be read.",
    "extracting": "Reading your report.",
    "extraction_complete": "Report read. Starting verification.",
    "auditing": "Double-checking what we read against your document.",
    "reconciling": "Finishing up.",
}


def _processing_state(report: CreditReport) -> dict[str, Any]:
    stage = report.processing_stage
    parsed = report.parsed_data or {}
    return {
        "report_id": str(report.id),
        "processing_stage": stage,
        "processing": is_in_flight(report),
        "processing_message": _STAGE_MESSAGES.get(stage),
        "processing_started_at": report.processing_started_at.isoformat() if report.processing_started_at else None,
        "processing_finished_at": (
            report.processing_finished_at.isoformat() if report.processing_finished_at else None
        ),
        "attempt_count": report.attempt_count or 0,
        "extraction_status": report.extraction_status,
        "extraction_reasons": parsed.get("extraction_reasons") or [],
        "warnings": parsed.get("warnings") or [],
    }


def _report_summary(report: CreditReport) -> dict[str, Any]:
    parsed = report.parsed_data or {}
    quality = parsed.get("extraction_quality") or {}
    return {
        "id": str(report.id),
        "bureau": report.bureau,
        "credit_score": report.credit_score,
        "score_type": report.score_type,
        "on_file_since": report.on_file_since,
        "report_date": report.report_date.date().isoformat() if report.report_date else None,
        "pull_date": report.pull_date.isoformat() if report.pull_date else None,
        "source": report.source,
        "extraction_method": parsed.get("extraction_method"),
        # Explicit quality state, so the UI never shows a clean "uploaded"
        # result for a report we couldn't read properly.
        "extraction_status": report.extraction_status,
        "extraction_verified": report.extraction_status == ExtractionStatus.VERIFIED.value,
        "extraction_complete": bool(quality.get("complete")),
        # Whether re-running extraction on the stored original could help.
        # Status only — this summary is used by the list endpoint, where the
        # accounts relationship isn't loaded; the retry route does the rest of
        # the checking.
        "extraction_retryable": ExtractionStatus(report.extraction_status).is_retryable,
        # Counted at persist time for the same reason: no lazy load here.
        "total_accounts": parsed.get("account_count", 0),
        "total_inquiries": parsed.get("inquiry_count", 0),
        **_processing_state(report),
    }
