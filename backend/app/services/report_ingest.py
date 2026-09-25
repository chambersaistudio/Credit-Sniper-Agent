"""
What one ingestion attempt produced, and how it is written to a report.

Split out of the API layer because extraction no longer happens inside the
upload request: the route stores the document and enqueues, and a background
worker (app.services.extraction_jobs) runs the expensive passes. Both need
the same outcome shape, bureau resolution, persistence and consumer-facing
warnings, so they live here rather than in either caller.

No credit judgment happens here — only faithful storage of what the document
was read to say, plus an explicit quality state.
"""
import logging
import uuid
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.credit_report import CreditAccount, CreditInquiry, CreditReport
from app.models.user import User
from app.services.account_matcher import link_accounts
from app.services.ai import AIError
from app.services.ai_extraction import extract_with_ai
from app.services.document_extraction import ExtractionStatus
from app.services.document_extraction.status import OPERATIONAL_MESSAGES
from app.services.document_extraction.mapping import account_row, inquiry_rows, public_records
from app.services.document_extraction.pipeline import DocumentExtractionResult
from app.services.extraction_quality import assess_accounts, inquiry_is_suspicious
from app.services.redaction import Identity
from app.utils.dates import parse_report_date

logger = logging.getLogger(__name__)

SUPPORTED_BUREAUS = {"equifax", "experian", "transunion"}

# Said to the consumer when the AI provider — not their document — failed.
# Deliberately free of provider internals: quota, rate limits and 5xx are our
# operational problem, and the detail belongs in logs and telemetry.
PROVIDER_UNAVAILABLE_MESSAGE = OPERATIONAL_MESSAGES[ExtractionStatus.PROVIDER_UNAVAILABLE]
UNKNOWN_BUREAU_MESSAGE = (
    "We couldn't tell which bureau this report is from. Re-upload it and choose the bureau."
)

ACCOUNT_COLUMNS = (
    "creditor_name", "original_creditor", "sold_to", "account_number", "account_type",
    "account_status", "account_status_raw", "account_lifecycle", "payment_performance",
    "payment_status", "report_classification",
    "balance", "past_due_amount", "high_balance", "credit_limit", "original_amount", "monthly_payment",
    "terms", "responsibility", "consumer_dispute",
    "date_opened", "date_closed", "date_of_first_delinquency", "date_last_reported",
    "date_last_payment", "date_last_active", "date_status_updated", "balance_updated_date", "remarks",
    "payment_history", "contact", "source_pages", "field_evidence",
)
# Parser fields kept only in raw_data for traceability (no dedicated column).
_RAW_ONLY = ("raw_block", "extraction_method")


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


def build_account(report_id: uuid.UUID, bureau: str, raw: dict[str, Any]) -> CreditAccount:
    raw_data = {**(raw.get("raw_data") or {}), **{key: raw[key] for key in _RAW_ONLY if key in raw}}
    return CreditAccount(
        report_id=report_id,
        bureau=bureau,
        raw_data=raw_data or None,
        **{column: raw.get(column) for column in ACCOUNT_COLUMNS},
    )


def resolve_bureau(requested: str, detected: str | None) -> str | None:
    """The bureau to file this report under, or None when nothing identified
    one. Callers decide what an unidentified bureau means for them: the upload
    route can still reject outright, the worker records it as a quality
    problem rather than losing the stored document."""
    bureau = (detected or "").strip().lower() if requested == "auto_detect" else requested
    return bureau if bureau in SUPPORTED_BUREAUS else None


def cross_check(parsed: dict[str, Any], account_count: int) -> dict[str, Any]:
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


def outcome_from_document(result: DocumentExtractionResult, parsed: dict[str, Any]) -> IngestOutcome:
    """Turn a completed two-pass document reading into an ingest outcome.

    Takes an already-run result so the caller controls when each expensive
    pass happens — a failed audit must never cause a second extraction."""
    if result.extraction is None:
        # Distinguish an operational failure — the provider was down, or the
        # model's answer was unusable — from "we read the document and
        # couldn't make sense of it". Only the latter says anything about the
        # consumer's PDF, and only it may be reported as such.
        return IngestOutcome(
            accounts=[], inquiries=[], quality=assess_accounts([]), status=result.status,
            method=result.status.value if result.status.is_operational else "document_failed",
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
        cross_check=cross_check(parsed, len(accounts)),
    )


async def ingest_with_parser(
    parsed: dict[str, Any], raw_text: str, user: User | None, user_id: uuid.UUID
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


async def persist_outcome(
    db: AsyncSession, report: CreditReport, outcome: IngestOutcome, parsed: dict[str, Any],
    chosen_bureau: str, user_id: uuid.UUID,
) -> tuple[list[CreditAccount], list[dict[str, Any]]]:
    """Write one extraction outcome onto a report. Shared by the worker and
    the retry path so a retried extraction lands exactly like a first one."""
    raw_inquiries = [inq for inq in outcome.inquiries if not inquiry_is_suspicious(inq)]

    report.bureau = chosen_bureau
    report.credit_score = outcome.credit_score
    report.score_type = outcome.score_type
    report.on_file_since = outcome.on_file_since
    report.report_date = to_datetime(outcome.report_date)
    report.extraction_status = outcome.status.value
    report.extraction_audit = outcome.audit
    report.public_records = outcome.public_records

    accounts = [build_account(report.id, chosen_bureau, raw) for raw in outcome.accounts]
    report.parsed_data = {
        **{k: v for k, v in (report.parsed_data or {}).items() if k in ("requested_bureau",)},
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
        # Counts and warnings are read back by the status endpoint. Extraction
        # is asynchronous now, so the upload response can't carry them and the
        # list endpoint must not lazy-load the accounts relationship to
        # rebuild them.
        "account_count": len(accounts),
        "inquiry_count": len(raw_inquiries),
        "warnings": warnings_for(outcome, len(accounts)),
    }

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


def warnings_for(outcome: IngestOutcome, account_count: int) -> list[str]:
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
    if outcome.status.is_operational:
        # The document was never analyzed, so nothing may be said about what
        # it contains — least of all that it holds no accounts. Each
        # operational failure gets its own copy: telling a consumer to "retry
        # once the service is available" after a billed truncation would be
        # both untrue and expensive.
        warnings.append(OPERATIONAL_MESSAGES[outcome.status])
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


def to_datetime(value: str | None) -> datetime | None:
    parsed = parse_report_date(value)
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc) if parsed else None
