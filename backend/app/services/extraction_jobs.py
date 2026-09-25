"""
Durable background extraction.

A real Experian PDF takes longer to read than a browser or a proxy will hold
a connection open. Running extraction inside the upload request meant a
dropped connection looked like a failure to the consumer while we had already
paid the provider for the work. So the request only stores the document and
enqueues; a worker does the reading.

    queued -> extracting -> extraction_complete -> auditing -> reconciling
           -> verified | needs_audit | extraction_incomplete
            | failed | provider_unavailable

Every expensive pass is checkpointed the moment it succeeds:

  * extraction succeeded, audit failed  -> the audit is retried alone; the
    extractor is NOT called again
  * audit succeeded, persistence failed -> neither model is called again

State lives in Postgres, so a restart resumes rather than repeats. Terminal
stages are the ExtractionStatus values themselves, so the stage and the
quality state can never disagree.
"""
import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.database import async_session_maker
from app.models.credit_report import CreditReport
from app.models.user import User
from app.services.document_extraction import ExtractionStatus
from app.services.document_extraction.pipeline import (
    DocumentExtractionResult,
    reconcile,
    run_auditor,
    run_extractor,
)
from app.services.document_extraction.schema import AuditReport, CreditReportExtraction
from app.services.pdf_parser import parse_credit_report_pdf
from app.services.report_ingest import (
    UNKNOWN_BUREAU_MESSAGE,
    ingest_with_parser,
    outcome_from_document,
    persist_outcome,
    resolve_bureau,
)
from app.services.storage import get_storage

logger = logging.getLogger(__name__)


class Stage:
    QUEUED = "queued"
    EXTRACTING = "extracting"
    EXTRACTION_COMPLETE = "extraction_complete"
    AUDITING = "auditing"
    RECONCILING = "reconciling"


# Stages that mean a job is still owed work. Everything else — every
# ExtractionStatus value — is terminal.
PENDING_STAGES = (Stage.QUEUED, Stage.EXTRACTING, Stage.EXTRACTION_COMPLETE,
                  Stage.AUDITING, Stage.RECONCILING)
# A worker may pick up any pending stage: EXTRACTING and AUDITING are included
# so a job interrupted mid-pass by a restart resumes from its last checkpoint
# instead of being abandoned.
CLAIMABLE_STAGES = PENDING_STAGES


def document_sha256(content: bytes) -> str:
    """Idempotency key for an uploaded original. Repeated clicks on the same
    file must not create a second billable job."""
    return hashlib.sha256(content).hexdigest()


def is_in_flight(report: CreditReport) -> bool:
    return report.processing_stage in PENDING_STAGES


async def _set_stage(db: AsyncSession, report: CreditReport, stage: str) -> None:
    report.processing_stage = stage
    await db.commit()


def enqueue(report: CreditReport, *, reset_audit: bool = False) -> None:
    """Mark a report as owed work. Never clears the extraction checkpoint: a
    retry after a failed audit must reuse the extraction we already paid for.

    `reset_audit` drops only a failed audit pass, so the second model is
    called again while the first is not."""
    checkpoint = dict(report.extraction_checkpoint or {})
    if reset_audit and checkpoint.get("audit_error"):
        checkpoint.pop("audit_done", None)
        checkpoint.pop("audit", None)
        checkpoint.pop("audit_error", None)
        report.extraction_checkpoint = checkpoint
    report.processing_stage = Stage.QUEUED
    report.processing_finished_at = None
    report.last_processing_error_class = None


async def process_report(report_id, *, session_factory=None) -> str:
    """Run (or resume) one report's extraction. Returns the final stage.

    Safe to call repeatedly: each pass is skipped when its checkpoint already
    exists, so a retry after a provider outage costs only the pass that
    actually failed."""
    factory = session_factory or async_session_maker
    async with factory() as db:
        report = await db.get(CreditReport, report_id)
        if report is None:
            return ExtractionStatus.FAILED.value
        if not is_in_flight(report):
            return report.processing_stage

        report.attempt_count = (report.attempt_count or 0) + 1
        report.processing_started_at = report.processing_started_at or datetime.now(timezone.utc)
        report.last_processing_error_class = None
        await _set_stage(db, report, report.processing_stage or Stage.QUEUED)

        try:
            document = await get_storage().get(report.storage_key)
        except Exception as e:
            return await _fail(db, report, e, "Couldn't read the stored original for this report.")

        # The deterministic parser runs locally and costs nothing: it supplies
        # personal info, page count and an independent tradeline count used
        # only as a cross-check. It never overrides the document model.
        try:
            parsed = await run_in_threadpool(parse_credit_report_pdf, document)
        except Exception as e:
            return await _fail(db, report, e, "Couldn't read this PDF.")

        if not settings.document_extraction_enabled:
            user = await db.get(User, report.user_id)
            outcome = await ingest_with_parser(parsed, report.raw_text or "", user, report.user_id)
            return await _finish(db, report, outcome, parsed)

        # The checkpoint is treated as immutable and rebuilt on every write.
        # A plain JSON column tracks changes by comparing the old value to the
        # new one, so mutating the dict already stored on the row would leave
        # the update invisible to the flush — and a pass we paid for unsaved.
        checkpoint = dict(report.extraction_checkpoint or {})
        context = {"user_id": str(report.user_id), "report_id": str(report.id)}

        # ── Pass 1: extract. Skipped entirely if already checkpointed. ──
        if not checkpoint.get("extraction"):
            await _set_stage(db, report, Stage.EXTRACTING)
            extraction, model, error = await run_extractor(document, context=context)
            if extraction is None:
                return await _provider_unavailable(db, report, parsed, error)
            checkpoint = {**checkpoint,
                          "extraction": extraction.model_dump(mode="json"),
                          "extractor_model": model}
            report.extraction_checkpoint = checkpoint
            await _set_stage(db, report, Stage.EXTRACTION_COMPLETE)
        extraction = CreditReportExtraction.model_validate(checkpoint["extraction"])

        # ── Pass 2: audit. Getting here never re-runs pass 1. ──
        audit = None
        if settings.document_audit_enabled and not checkpoint.get("audit_done"):
            await _set_stage(db, report, Stage.AUDITING)
            audit, model, error = await run_auditor(document, extraction, context=context)
            checkpoint = {**checkpoint,
                          "audit": audit.model_dump(mode="json") if audit else None,
                          "audit_done": True,
                          "auditor_model": model,
                          "audit_error": error}
            report.extraction_checkpoint = checkpoint
        elif checkpoint.get("audit"):
            audit = AuditReport.model_validate(checkpoint["audit"])

        # ── Reconcile and persist. Calls no model, so a failure here is free
        # to retry: both checkpoints are already on the row. ──
        await _set_stage(db, report, Stage.RECONCILING)
        status, reasons = reconcile(extraction, audit)
        result = DocumentExtractionResult(
            extraction=extraction, audit=audit, status=status, reasons=reasons,
            extractor_model=checkpoint.get("extractor_model"),
            auditor_model=checkpoint.get("auditor_model"),
            provider_error=checkpoint.get("audit_error"),
        )
        outcome = outcome_from_document(result, parsed)
        return await _finish(db, report, outcome, parsed)


async def _finish(db: AsyncSession, report: CreditReport, outcome, parsed: dict[str, Any]) -> str:
    """Persist one outcome and land the report on a terminal stage."""
    requested = (report.parsed_data or {}).get("requested_bureau") or "auto_detect"
    bureau = resolve_bureau(requested, outcome.bureau)
    if bureau is None:
        # Nothing identified the bureau. The document is still stored and the
        # consumer can re-upload naming it; we don't guess.
        outcome.status = ExtractionStatus.EXTRACTION_INCOMPLETE
        outcome.reasons = [*outcome.reasons, UNKNOWN_BUREAU_MESSAGE]
        bureau = "unknown"

    try:
        await persist_outcome(db, report, outcome, parsed, bureau, report.user_id)
    except Exception as e:
        await db.rollback()
        report = await db.get(CreditReport, report.id)
        return await _fail(db, report, e, "Couldn't store the extracted report data.")

    report.processing_finished_at = datetime.now(timezone.utc)
    await _set_stage(db, report, outcome.status.value)
    return outcome.status.value


async def _provider_unavailable(
    db: AsyncSession, report: CreditReport, parsed: dict[str, Any], error: str | None
) -> str:
    """The provider failed us before anything was read. The stored document is
    untouched and the job can be retried later without re-running any pass
    that already succeeded."""
    report.last_processing_error_class = (error or "AIError").split(":", 1)[0]
    result = DocumentExtractionResult(
        None, None, ExtractionStatus.PROVIDER_UNAVAILABLE,
        ["AI extraction was unavailable, so the document was never analyzed."],
        provider_error=error,
    )
    return await _finish(db, report, outcome_from_document(result, parsed), parsed)


async def _fail(db: AsyncSession, report: CreditReport, error: Exception, reason: str) -> str:
    """An infrastructure failure, not a verdict on the document. The provider
    error class is kept for operators; consumers only see `reason`."""
    logger.exception("Extraction job failed for report %s", getattr(report, "id", None))
    report.last_processing_error_class = type(error).__name__
    report.extraction_status = ExtractionStatus.FAILED.value
    report.parsed_data = {
        **(report.parsed_data or {}),
        "extraction_reasons": [reason],
        "warnings": [reason],
    }
    report.processing_finished_at = datetime.now(timezone.utc)
    await _set_stage(db, report, ExtractionStatus.FAILED.value)
    return ExtractionStatus.FAILED.value


async def claim_next(session_factory=None) -> Any | None:
    """The id of one report still owed work, oldest first."""
    factory = session_factory or async_session_maker
    async with factory() as db:
        result = await db.execute(
            select(CreditReport.id)
            .where(CreditReport.processing_stage.in_(CLAIMABLE_STAGES))
            .order_by(CreditReport.created_at)
            .limit(1)
        )
        return result.scalars().first()


async def worker_loop(poll_seconds: float = 2.0) -> None:
    """Single in-process worker. All state is in Postgres, so a restart
    resumes from the last checkpoint rather than paying for the work again."""
    logger.info("Extraction worker started")
    while True:
        try:
            report_id = await claim_next()
            if report_id is None:
                await asyncio.sleep(poll_seconds)
                continue
            await process_report(report_id)
        except asyncio.CancelledError:
            logger.info("Extraction worker stopping")
            raise
        except Exception:
            logger.exception("Extraction worker iteration failed")
            await asyncio.sleep(poll_seconds)
