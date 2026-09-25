"""
Credit report ingestion. Upload stores exactly what the document says —
parsed deterministically, or via a source-verified AI extraction fallback
when the layout defeats the parser — then links each tradeline to its
cross-bureau canonical account. No dispute judgment happens here.
"""
import logging
import uuid
from dataclasses import dataclass
from dataclasses import field as dataclass_field
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
from app.services.document_extraction import ExtractionStatus, extract_document
from app.services.document_extraction.mapping import account_row, inquiry_rows, public_records
from app.services.extraction_quality import assess_accounts, inquiry_is_suspicious
from app.services.pdf_parser import parse_credit_report_pdf
from app.services.redaction import Identity
from app.services.storage import get_storage, report_key
from app.utils.dates import parse_report_date
from app.utils.default_user import parse_uuid

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/reports", tags=["reports"])

SUPPORTED_BUREAUS = {"equifax", "experian", "transunion"}

# Said to the consumer when the AI provider — not their document — failed.
# Deliberately free of provider internals: quota, rate limits and 5xx are our
# operational problem, and the detail belongs in logs and telemetry.
PROVIDER_UNAVAILABLE_MESSAGE = (
    "Your report was stored safely, but AI extraction is temporarily unavailable. "
    "No report data was analyzed. Retry extraction once the service is available."
)
MAX_SIZE_BYTES = settings.max_file_size_mb * 1024 * 1024
PDF_MAGIC = b"%PDF-"

_ACCOUNT_COLUMNS = (
    "creditor_name", "original_creditor", "sold_to", "account_number", "account_type",
    "account_status", "account_status_raw", "payment_status", "report_classification",
    "balance", "past_due_amount", "high_balance", "credit_limit", "original_amount", "monthly_payment",
    "terms", "responsibility", "consumer_dispute",
    "date_opened", "date_closed", "date_of_first_delinquency", "date_last_reported",
    "date_last_payment", "date_last_active", "date_status_updated", "balance_updated_date", "remarks",
    "payment_history", "contact", "source_pages", "field_evidence",
)
# Parser fields kept only in raw_data for traceability (no dedicated column).
_RAW_ONLY = ("raw_block", "extraction_method")


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
    raw_data = {**(raw.get("raw_data") or {}), **{key: raw[key] for key in _RAW_ONLY if key in raw}}
    return CreditAccount(
        report_id=report_id,
        bureau=bureau,
        raw_data=raw_data or None,
        **{column: raw.get(column) for column in _ACCOUNT_COLUMNS},
    )


@dataclass
class IngestOutcome:
    """What one ingestion strategy produced, before it is written to the DB."""

    accounts: list[dict[str, Any]]
    inquiries: list[dict[str, Any]]
    quality: Any
    status: ExtractionStatus
    method: str
    bureau: str = "unknown"
    credit_score: int | None = None
    score_type: str | None = None
    report_date: str | None = None
    on_file_since: str | None = None
    reasons: list[str] = dataclass_field(default_factory=list)
    audit: dict[str, Any] | None = None
    public_records: list[dict[str, Any]] | None = None
    cross_check: dict[str, Any] | None = None
    ungrounded_values_dropped: int = 0
    redactions: dict[str, int] = dataclass_field(default_factory=dict)


def _cross_check(parsed: dict[str, Any], account_count: int) -> dict[str, Any]:
    """The deterministic parser as a second opinion only. It records whether
    it saw the same number of tradelines; a mismatch is information, not a
    decision — the parser never overrides the document model."""
    parser_accounts = [
        a for a in parsed.get("accounts_raw", [])
        if a.get("extraction_method") != "full_text_ai_parse"
    ]
    parser_quality = assess_accounts(parser_accounts)
    return {
        "parser_accounts": len(parser_accounts),
        "document_accounts": account_count,
        "agrees_on_count": len(parser_accounts) == account_count,
        "parser_complete": parser_quality.complete,
    }


async def _ingest_with_document_model(
    document: bytes, parsed: dict[str, Any], requested_bureau: str, user_id: uuid.UUID
) -> IngestOutcome:
    """Authoritative path: a vision model reads the ORIGINAL PDF, a second
    pass audits that reading against the same PDF, and a deterministic gate
    decides whether the result may be relied on."""
    result = await extract_document(document, context={"user_id": str(user_id)})
    if result.extraction is None:
        # Distinguish "the provider was unavailable" from "we read the
        # document and couldn't make sense of it". Only the latter says
        # anything about the consumer's PDF.
        provider_failure = result.status is ExtractionStatus.PROVIDER_UNAVAILABLE
        return IngestOutcome(
            accounts=[], inquiries=[], quality=assess_accounts([]), status=result.status,
            method="provider_unavailable" if provider_failure else "document_failed",
            reasons=result.reasons, audit=result.audit_to_dict(),
            bureau=parsed.get("bureau", "unknown"),
        )

    extraction = result.extraction
    accounts = [account_row(t) for t in extraction.accounts]
    quality = assess_accounts(accounts)
    status, reasons = result.status, list(result.reasons)
    # The document passes cleanly but the rows still don't look like
    # tradelines: hold it back rather than call it verified.
    if status is ExtractionStatus.VERIFIED and not quality.complete:
        status = ExtractionStatus.EXTRACTION_INCOMPLETE
        reasons += quality.reasons

    return IngestOutcome(
        accounts=accounts,
        inquiries=inquiry_rows(extraction),
        quality=quality,
        status=status,
        method="ai_document",
        bureau=(extraction.bureau or parsed.get("bureau", "unknown") or "unknown").strip().lower(),
        credit_score=extraction.score if extraction.score is None or 300 <= extraction.score <= 850 else None,
        score_type=extraction.score_type,
        # Recency comes from the document's own creation date when it prints
        # one. A bureau's "on file since" date is historical metadata and is
        # kept separately — it is not when this report was produced.
        report_date=(extraction.document_created_date or extraction.report_date
                     or parsed.get("report_date")),
        on_file_since=extraction.consumer_on_file_since,
        reasons=reasons,
        audit=result.audit_to_dict(),
        public_records=public_records(extraction),
        cross_check=_cross_check(parsed, len(accounts)),
    )


async def _ingest_with_parser(
    parsed: dict[str, Any], raw_text: str, user: User, user_id: uuid.UUID
) -> IngestOutcome:
    """Fallback path when no document-understanding provider is configured,
    or when the document model failed.

    This path can NEVER reach VERIFIED. VERIFIED means the original PDF was
    read by the document model, independently audited against that same PDF,
    and reconciled — none of which happened here. A clean deterministic parse
    caps at NEEDS_AUDIT, which still blocks dispute analysis."""
    raw_accounts = parsed.get("accounts_raw", [])
    raw_inquiries = parsed.get("inquiries_raw", [])
    method = "parser"
    dropped = 0
    redactions: dict[str, int] = {}

    sentinel = len(raw_accounts) == 1 and raw_accounts[0].get("extraction_method") == "full_text_ai_parse"
    if sentinel:
        raw_accounts = []
    quality = assess_accounts(raw_accounts)

    if not quality.complete:
        identity = Identity.from_sources(parsed.get("personal_info"), user)
        try:
            extraction = await extract_with_ai(raw_text, identity, context={"user_id": str(user_id)})
            ai_quality = assess_accounts(extraction.accounts)
            if sentinel or ai_quality.substantial >= quality.substantial:
                raw_accounts = extraction.accounts
                raw_inquiries = extraction.inquiries or raw_inquiries
                dropped = extraction.ungrounded_values_dropped
                redactions = extraction.redactions
                quality = ai_quality
                method = "ai_verified" if ai_quality.complete else "ai_incomplete"
            else:
                method = "parser_incomplete"
        except AIError as e:
            logger.warning("AI extraction fallback failed: %s", e)
            method = "failed" if not raw_accounts else "parser_incomplete"

    if not raw_accounts:
        status = ExtractionStatus.FAILED
        reasons = quality.reasons
    elif quality.complete:
        # Capped deliberately: nothing verified this against the original PDF.
        status = ExtractionStatus.NEEDS_AUDIT
        reasons = ["Read by the deterministic parser only — not verified against the original document."]
    else:
        status = ExtractionStatus.EXTRACTION_INCOMPLETE
        reasons = quality.reasons

    return IngestOutcome(
        accounts=raw_accounts, inquiries=raw_inquiries, quality=quality, status=status, method=method,
        bureau=parsed.get("bureau", "unknown"), credit_score=parsed.get("credit_score"),
        report_date=parsed.get("report_date"), reasons=reasons,
        ungrounded_values_dropped=dropped, redactions=redactions,
    )


async def _persist_outcome(
    db: AsyncSession, report: CreditReport, outcome: "IngestOutcome", parsed: dict[str, Any],
    chosen_bureau: str, user_id: uuid.UUID,
) -> tuple[list[CreditAccount], list[dict[str, Any]]]:
    """Write one extraction outcome onto a report. Shared by upload and retry
    so a retried extraction lands exactly like a first one."""
    raw_inquiries = [inq for inq in outcome.inquiries if not inquiry_is_suspicious(inq)]

    report.bureau = chosen_bureau
    report.credit_score = outcome.credit_score
    report.score_type = outcome.score_type
    report.on_file_since = outcome.on_file_since
    report.report_date = _to_datetime(outcome.report_date)
    report.extraction_status = outcome.status.value
    report.extraction_audit = outcome.audit
    report.public_records = outcome.public_records
    report.parsed_data = {
        "personal_info": parsed.get("personal_info", {}),
        "pages": parsed.get("pages"),
        "extraction_method": outcome.method,
        "extraction_quality": outcome.quality.to_dict(),
        "extraction_reasons": outcome.reasons,
        "ungrounded_values_dropped": outcome.ungrounded_values_dropped,
        "redactions_before_ai": outcome.redactions,
        # The deterministic parser stays on as a cross-check, never as the
        # authority: a disagreement is recorded, not silently resolved.
        "parser_cross_check": outcome.cross_check,
    }

    accounts = [_build_account(report.id, chosen_bureau, raw) for raw in outcome.accounts]
    db.add_all(accounts)
    db.add_all(
        CreditInquiry(
            report_id=report.id,
            bureau=chosen_bureau,
            creditor_name=inq.get("creditor_name"),
            inquiry_date=inq.get("inquiry_date"),
            # No default: an inquiry is only hard when the document says so.
            inquiry_type=inq.get("inquiry_type"),
            inquiry_category=inq.get("inquiry_category"),
            business_type=inq.get("business_type"),
        )
        for inq in raw_inquiries
    )
    await db.flush()
    await link_accounts(db, user_id, accounts)
    return accounts, raw_inquiries


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

    raw_text: str = parsed.get("raw_text", "")
    if not raw_text.strip():
        raise HTTPException(
            status_code=422,
            detail="This PDF has no readable text (it may be a scanned image). Download the report as a text PDF.",
        )

    # The original PDF is the authoritative document. Store it privately
    # first, then read the exact stored bytes back and hand THOSE to the
    # document model — so what we analyse is provably what we kept, and no
    # locally derived text stands in for the report.
    report = CreditReport(
        user_id=resolved_user_id, bureau="unknown", source="manual_upload", raw_text=raw_text,
        extraction_status=ExtractionStatus.EXTRACTION_INCOMPLETE.value,
    )
    db.add(report)
    await db.flush()
    report.storage_key = report_key(resolved_user_id, report.id)
    storage = get_storage()
    try:
        await storage.put(report.storage_key, content, "application/pdf")
        document = await storage.get(report.storage_key)
    except Exception:
        logger.exception("Storing report PDF failed")
        raise HTTPException(status_code=503, detail="Couldn't store the report file. Try again.")

    if settings.document_extraction_enabled:
        outcome = await _ingest_with_document_model(document, parsed, bureau, resolved_user_id)
    else:
        outcome = await _ingest_with_parser(parsed, raw_text, user, resolved_user_id)

    chosen_bureau = _choose_bureau(bureau, outcome.bureau)
    accounts, raw_inquiries = await _persist_outcome(db, report, outcome, parsed, chosen_bureau, resolved_user_id)
    quality = outcome.quality
    await db.commit()

    return {
        "report_id": str(report.id),
        "user_id": str(resolved_user_id),
        "bureau": chosen_bureau,
        "credit_score": report.credit_score,
        "score_type": report.score_type,
        "report_date": outcome.report_date,
        "extraction_method": outcome.method,
        "extraction_status": report.extraction_status,
        "total_accounts": len(accounts),
        "total_inquiries": len(raw_inquiries),
        "extraction_complete": quality.complete,
        "warnings": _warnings(outcome, len(accounts)),
    }


def _warnings(outcome: "IngestOutcome", account_count: int) -> list[str]:
    warnings = []
    if outcome.method in ("ai_verified", "ai_incomplete"):
        warnings.append(
            "This report's layout wasn't recognized, so accounts were extracted with AI and each value "
            "was checked against the document text. Review the accounts for accuracy."
        )
        if outcome.ungrounded_values_dropped:
            warnings.append(
                f"{outcome.ungrounded_values_dropped} extracted value(s) didn't appear in the document "
                "and were left blank."
            )
    if outcome.status is ExtractionStatus.PROVIDER_UNAVAILABLE:
        # The document was never analyzed, so nothing may be said about what
        # it contains — least of all that it holds no accounts.
        warnings.append(PROVIDER_UNAVAILABLE_MESSAGE)
    elif outcome.status is ExtractionStatus.FAILED or account_count == 0:
        warnings.append("We couldn't reliably read this PDF, so no accounts were extracted from it.")
    elif outcome.status is ExtractionStatus.NEEDS_AUDIT:
        # Read fine; the verification pass disagreed. Don't suggest re-uploading.
        warnings.append(
            "This report was read successfully, but the verification pass found unresolved extraction "
            "differences. Dispute analysis is paused until those differences are reconciled. "
            + " ".join(outcome.reasons)
        )
    elif outcome.status is not ExtractionStatus.VERIFIED:
        warnings.append(
            "This report couldn't be read completely, so dispute evaluation is blocked until it is "
            "re-ingested. " + " ".join(outcome.reasons or outcome.quality.reasons)
        )
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
    audit = report.extraction_audit or {}
    return {
        **_report_summary(report),
        # Everything needed to audit the extraction: the values read, where
        # they came from, and what the second pass said about them.
        "accounts": [
            {"id": str(a.id), **{column: getattr(a, column) for column in _ACCOUNT_COLUMNS}}
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


@router.post("/{report_id}/retry-extraction", response_model=dict[str, Any])
async def retry_extraction(
    report_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Re-run extraction against the original PDF already stored for this
    report, so a provider outage doesn't make the consumer upload the same
    file again.

    Only offered while the report holds no extracted accounts — that is the
    outage case. Once accounts exist they may already carry evaluations and
    cases, and replacing them is a different (destructive) operation."""
    report = await _owned_report(db, report_id, user.id)
    status = ExtractionStatus(report.extraction_status)
    if not status.is_retryable:
        raise HTTPException(status_code=409, detail="This report doesn't need re-extraction.")
    if report.accounts:
        raise HTTPException(
            status_code=409,
            detail="This report already has extracted accounts. Upload the report again to replace them.",
        )
    if not report.storage_key:
        raise HTTPException(status_code=409, detail="The original file for this report is no longer available.")

    try:
        document = await get_storage().get(report.storage_key)
    except Exception:
        logger.exception("Reading stored report for retry failed")
        raise HTTPException(status_code=503, detail="Couldn't read the stored report file. Try again.")

    try:
        parsed = await run_in_threadpool(parse_credit_report_pdf, document)
    except Exception as e:
        logger.exception("PDF parse failed on retry")
        raise HTTPException(status_code=422, detail=f"Could not read this PDF: {e}")

    if settings.document_extraction_enabled:
        outcome = await _ingest_with_document_model(document, parsed, report.bureau, user.id)
    else:
        outcome = await _ingest_with_parser(parsed, report.raw_text or "", user, user.id)

    # Keep the bureau already on the report if this attempt can't tell.
    bureau = outcome.bureau if outcome.bureau in SUPPORTED_BUREAUS else report.bureau
    # Old inquiries would otherwise be duplicated by a successful retry.
    for inquiry in list(report.inquiries):
        await db.delete(inquiry)
    await db.flush()

    accounts, raw_inquiries = await _persist_outcome(db, report, outcome, parsed, bureau, user.id)
    await db.commit()

    return {
        "report_id": str(report.id),
        "bureau": bureau,
        "extraction_status": report.extraction_status,
        "extraction_method": outcome.method,
        "total_accounts": len(accounts),
        "total_inquiries": len(raw_inquiries),
        "extraction_complete": outcome.quality.complete,
        "warnings": _warnings(outcome, len(accounts)),
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
    parsed = report.parsed_data or {}
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
        # Whether re-running extraction on the stored original could help.
        # Status only — this summary is used by the list endpoint, where the
        # accounts relationship isn't loaded; the retry route does the rest of
        # the checking.
        "extraction_retryable": ExtractionStatus(report.extraction_status).is_retryable,
        "extraction_reasons": parsed.get("extraction_reasons") or [],
    }


def _to_datetime(value: str | None) -> datetime | None:
    parsed = parse_report_date(value)
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc) if parsed else None
