"""
The handlers behind each allowlisted operation.

Every one of these calls the services we already built and tested from the
command line. The control plane adds durability, authentication and
accounting; it does not add a second implementation of extraction, which
would be a second thing to keep correct.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.credit_report import CreditReport
from app.models.operator_job import OperatorJob
from app.services.batch_job import batch_plans, extract_batch, run_report_batch
from app.services.benchmark.batch_scoring import score_batch
from app.services.index_job import index_report
from app.services.extraction_jobs import Stage, process_report
from app.services.document_extraction import ExtractionStatus
from app.services.operator import reads
from app.services.operator import truth as truth_service
from app.services.operator.truth import DEFAULT_LABEL
from app.services.document_extraction.scaled_merge import (
    merge_scaled_checkpoint, scaled_model_label,
)
from app.services.operator.jobs import handler
from app.services.operator.registry import config_model
from app.services.storage import get_storage

logger = logging.getLogger(__name__)


@handler("index_report")
async def index_report_job(db: AsyncSession, job: OperatorJob, request: dict) -> dict[str, Any]:
    result = await index_report(
        request["report_id"],
        expected_tradelines=request.get("expected_tradelines"),
        force=bool(request.get("force", False)),
    )
    if result.index is None:
        raise _failure_as_exception(result.failure)
    if not result.quality.ok:
        raise ValueError("; ".join(result.quality.reasons))
    return result.to_dict()


@handler("bank_batch")
async def bank_batch(db: AsyncSession, job: OperatorJob, request: dict) -> dict[str, Any]:
    result = await run_report_batch(
        request["report_id"],
        request["batch_id"],
        batch_size=request.get("batch_size", 4),
        context_pages=request.get("context_pages", 1),
        force=bool(request.get("force", False)),
        model="gpt-5.6-luna",
        detail="high",
    )
    if result.batch is None:
        raise _failure_as_exception(result.failure)
    if not result.quality.ok:
        raise ValueError("; ".join(result.quality.reasons))
    return result.to_dict()


_AUDIT_CHECKPOINT_KEYS = (
    "audit", "audit_done", "auditor_model", "audit_error", "audit_failure",
)


@handler("merge_scaled_report")
async def merge_scaled_report(db: AsyncSession, job: OperatorJob, request: dict) -> dict[str, Any]:
    """Assemble Stage 1 + Stage 2, but never bless the merge as verified.

    Replacing the extraction invalidates any audit of an older extraction.
    The report is deliberately parked at NEEDS_AUDIT until the independent
    finalizer reads the original PDF again.
    """
    report = await reads.get_report(db, request["report_id"])
    checkpoint = dict(report.extraction_checkpoint or {})
    extraction = merge_scaled_checkpoint(checkpoint)
    for key in _AUDIT_CHECKPOINT_KEYS:
        checkpoint.pop(key, None)
    checkpoint = {
        **checkpoint,
        "extraction": extraction.model_dump(mode="json"),
        "extractor_model": scaled_model_label(checkpoint),
    }
    report.extraction_checkpoint = checkpoint
    report.extraction_status = ExtractionStatus.NEEDS_AUDIT.value
    report.processing_stage = ExtractionStatus.NEEDS_AUDIT.value
    report.processing_finished_at = datetime.now(timezone.utc)
    report.processing_claimed_at = None
    report.last_processing_error_class = None
    await db.flush()
    return {
        "report_id": str(report.id),
        "accounts": len(extraction.accounts),
        "inquiries": len(extraction.inquiries),
        "public_records": len(extraction.public_records),
        "model": checkpoint["extractor_model"],
        "merged": True,
        "audit_required": True,
    }


@handler("finalize_scaled_report")
async def finalize_scaled_report(db: AsyncSession, job: OperatorJob, request: dict) -> dict[str, Any]:
    """Run the existing independent PDF audit + deterministic reconciliation.

    The production extraction worker already has the audited persistence path.
    We claim this report for this job, resume at EXTRACTION_COMPLETE, and call
    that same implementation. The operator runner's model-call meter wraps the
    whole handler, so the Sol audit remains a one-call paid operation.
    """
    report = await reads.get_report(db, request["report_id"])
    checkpoint = dict(report.extraction_checkpoint or {})
    if not checkpoint.get("extraction"):
        raise ValueError("scaled finalization requires a merged extraction")
    if not str(checkpoint.get("extractor_model") or "").startswith("scaled:"):
        raise ValueError("scaled finalization refuses a non-scaled extraction")

    if checkpoint.get("audit_done"):
        audit = checkpoint.get("audit") or {}
        return {
            "report_id": str(report.id),
            "status": report.extraction_status,
            "processing_stage": report.processing_stage,
            "auditor_model": checkpoint.get("auditor_model"),
            "audit_verified": audit.get("verified"),
            "findings": len(audit.get("findings") or []),
            "reused": True,
        }

    # Claim it ourselves so the ordinary background worker cannot race this
    # explicit operator finalization after we commit the pending stage.
    report.processing_stage = Stage.EXTRACTION_COMPLETE
    report.processing_finished_at = None
    report.processing_claimed_at = datetime.now(timezone.utc)
    report.last_processing_error_class = None
    await db.commit()

    final_stage = await process_report(report.id)
    # process_report used its own session; refresh this one from Postgres.
    await db.refresh(report)
    checkpoint = dict(report.extraction_checkpoint or {})
    audit = checkpoint.get("audit") or {}
    return {
        "report_id": str(report.id),
        "status": report.extraction_status,
        "processing_stage": final_stage,
        "auditor_model": checkpoint.get("auditor_model"),
        "audit_verified": audit.get("verified"),
        "findings": len(audit.get("findings") or []),
        "reused": False,
    }

@handler("benchmark_batch")
async def benchmark_batch(db: AsyncSession, job: OperatorJob, request: dict) -> dict[str, Any]:
    """Read ONE batch under ONE model and score it against confirmed truth.

    Everything this must not do is structural rather than careful:

    * it calls `extract_batch`, which touches no database, never
      `run_report_batch`, which is what banks — so a benchmark cannot become
      the report's extraction
    * the model and detail are set for the duration and restored in a
      `finally`, so a failure cannot leave production pointing at a
      benchmark's model
    * the banked batches are hashed before and after, and the result says
      whether they changed

    One config per job. There is no parameter that runs more than one, and no
    loop here that could.
    """
    report_id = request["report_id"]
    batch_id = request["batch_id"]
    config = request["config"]

    # Truth is resolved from the store at run time and checked against the
    # fingerprint recorded when the job was queued. A correction made in
    # between fails the job rather than quietly scoring against different
    # values than the operator asked for. Inline truth (the CLI path) travels
    # with the job because there is no stored row to resolve.
    if request.get("truth_inline"):
        truth_accounts = (request.get("truth") or {}).get("accounts") or []
    else:
        resolved = await truth_service.resolve_for_job(
            db, report_id, batch_id, request.get("truth_label", DEFAULT_LABEL),
            expected_fingerprint=request.get("truth_fingerprint"),
        )
        truth_accounts = resolved.get("accounts") or []
    if not truth_accounts:
        raise ValueError("benchmark truth must contain at least one account")

    model, detail = config_model(config)

    report = await reads.get_report(db, report_id)
    plans = batch_plans(report, batch_size=request.get("batch_size", 4),
                        context_pages=request.get("context_pages", 1))
    plan = next((p for p in plans if p.batch_id == batch_id), None)
    if plan is None:
        raise LookupError(f"no batch {batch_id!r}; this report has {[p.batch_id for p in plans]}")

    banked_before = _banked_digest(report)
    document = await get_storage().get(report.storage_key)

    # The model is an argument, not a global. This runs in the same process as
    # the consumer extraction worker, so mutating settings — even with a
    # finally that restores them — would leave a window in which an upload was
    # read by whichever model a benchmark happened to be testing.
    result = await extract_batch(document, plan, model=model, detail=detail, context={
        "operator_job": str(job.id), "benchmark_config": config,
    })

    if result.batch is None:
        failure = result.failure
        # Raised so the job records a safe error and the real class; the
        # provider's own wording stays in the log.
        raise _failure_as_exception(failure)

    card = score_batch(batch_id, config, result.accounts, truth_accounts, result.quality)
    truth_row = (None if request.get("truth_inline") else await truth_service.get(
        db, report_id, batch_id, request.get("truth_label", DEFAULT_LABEL)))

    # Re-read the row to prove nothing was written by this run.
    await db.refresh(report)
    banked_after = _banked_digest(report)

    return {
        "report_id": str(report.id),
        "batch_id": batch_id,
        "config": config,
        "truth_label": request.get("truth_label", DEFAULT_LABEL),
        "truth_fingerprint": request.get("truth_fingerprint"),
        "truth_drafted_by_model": truth_row.drafted_by_model if truth_row else None,
        # Set when this model is being scored against truth it drafted itself.
        "anchoring_warning": truth_service.anchoring_note(truth_row, result.model),
        "model": result.model,
        "detail": detail,
        "plan": plan.to_dict(),
        "bundle": result.bundle.to_dict() if result.bundle else None,
        "remap": result.remap.to_dict() if result.remap else None,
        **card.to_dict(),
        "accounts_read": [
            {
                "creditor_name": account.creditor_name,
                "original_creditor": account.original_creditor,
                "account_number": account.account_number,
                "account_type": account.account_type,
                "open_closed": account.open_closed,
                "status_raw": account.status_raw,
                "balance": account.balance,
                "past_due_amount": account.past_due_amount,
                "credit_limit": account.credit_limit,
                "date_opened": account.date_opened,
                "date_closed": account.date_closed,
                "source_pages": account.source_pages,
                "payment_history_months": len(account.payment_history or []),
            }
            for account in result.accounts
        ],
        "banked_batches_unchanged": banked_before == banked_after,
        "banked_batches": sorted((report.extraction_checkpoint or {}).get("batches") or {}),
    }


@handler("draft_truth_batch")
async def draft_truth_batch(db: AsyncSession, job: OperatorJob, request: dict) -> dict[str, Any]:
    """Read ONE batch under ONE model and store it as an UNVERIFIED truth draft.

    For a batch with nothing banked: the free draft needs a banked extraction,
    and the benchmark needs verified truth, so without this the only way in is
    to type the accounts by hand.

    Structurally the same guarantees as the benchmark — `extract_batch`, never
    `run_report_batch`; the banked batches hashed before and after — plus two
    of its own:

    * it refuses to overwrite stored truth, checked BEFORE the model call so a
      refusal costs nothing, and again before writing so a correction saved
      while the model was reading is never discarded
    * the result is stored `verified=False` with the drafting model recorded,
      so the benchmark refuses it until a human has checked it against the
      document, and afterwards flags a benchmark of this same model
    """
    report_id = request["report_id"]
    batch_id = request["batch_id"]
    config = request["config"]
    label = request.get("truth_label", DEFAULT_LABEL)

    if await truth_service.get(db, report_id, batch_id, label) is not None:
        raise truth_service.TruthExists(f"truth already stored for {batch_id}")

    model, detail = config_model(config)
    report = await reads.get_report(db, report_id)
    plans = batch_plans(report, batch_size=request.get("batch_size", 4),
                        context_pages=request.get("context_pages", 1))
    plan = next((p for p in plans if p.batch_id == batch_id), None)
    if plan is None:
        raise LookupError(f"no batch {batch_id!r}; this report has {[p.batch_id for p in plans]}")

    banked_before = _banked_digest(report)
    document = await get_storage().get(report.storage_key)
    result = await extract_batch(document, plan, model=model, detail=detail, context={
        "operator_job": str(job.id), "truth_draft_config": config,
    })
    if result.batch is None:
        raise _failure_as_exception(result.failure)

    drafted = truth_service.draft_from_batch({"batch": result.batch.model_dump(mode="json")})

    # Second check, now that the paid read is done: someone may have saved
    # truth by hand while the model was reading. Theirs wins.
    if await truth_service.get(db, report_id, batch_id, label) is not None:
        raise truth_service.TruthExists(
            f"truth for {batch_id} was saved while the draft was being read; kept it")

    row = await truth_service.upsert(
        db, report_id=report.id, batch_id=batch_id, accounts=drafted,
        created_by=job.requested_by or "operator", label=label, verified=False,
        source="drafted_from_model",
        note=f"Drafted by {result.model} ({config}). Correct every field against the document, "
             f"then verify. Not truth until then.",
        drafted_by_model=result.model, drafted_by_config=config,
    )
    # Not committed here: the runner commits it with the job, or rolls it back
    # if the job fails (including a broken call budget found after this returns).
    await db.refresh(report)
    rows = drafted["accounts"]
    # Counts, not values: the values live in the truth store, fetched only
    # when someone opens the editor to correct them.
    return {
        "report_id": str(report.id),
        "batch_id": batch_id,
        "config": config,
        "model": result.model,
        "detail": detail,
        "truth_label": label,
        "truth_fingerprint": row.fingerprint,
        "verified": False,
        "accounts_drafted": len(rows),
        "months_drafted": sum(len(r.get("payment_history") or {}) for r in rows),
        "gate": result.quality.to_dict(),
        "banked_batches_unchanged": banked_before == _banked_digest(report),
    }


def _banked_digest(report: CreditReport) -> str:
    return json.dumps((report.extraction_checkpoint or {}).get("batches") or {}, sort_keys=True)


def _failure_as_exception(failure) -> Exception:
    """Turn a classified pass failure into an exception the job can record.

    Keeps the original class name so `safe_error` maps it to the right
    operator wording — an outage and a billed truncation are different
    events and stay different here."""
    from app.services.ai import (
        AIConfigurationError, AIProviderError, AIRefusalError, AIResponseError,
    )

    classes = {
        "AIProviderError": AIProviderError,
        "AIResponseError": AIResponseError,
        "AIRefusalError": AIRefusalError,
        "AIConfigurationError": AIConfigurationError,
    }
    if failure is None:
        return RuntimeError("the batch produced no result")
    # The detail goes to the log via run_job's logger.exception, not to the
    # operator, who gets safe_error's wording.
    return classes.get(failure.error_class, RuntimeError)(failure.detail)


@handler("diagnose_report")
async def diagnose_report(db: AsyncSession, job: OperatorJob, request: dict) -> dict[str, Any]:
    report = await reads.get_report(db, request["report_id"])
    return await reads.diagnose(db, report)


@handler("inspect_checkpoint")
async def inspect_checkpoint(db: AsyncSession, job: OperatorJob, request: dict) -> dict[str, Any]:
    report = await reads.get_report(db, request["report_id"])
    return reads.inspect_checkpoint(report)


@handler("batch_plan")
async def batch_plan(db: AsyncSession, job: OperatorJob, request: dict) -> dict[str, Any]:
    report = await reads.get_report(db, request["report_id"])
    return reads.batch_plan(report, batch_size=request.get("batch_size", 4),
                            context_pages=request.get("context_pages", 1))


@handler("extraction_status")
async def extraction_status(db: AsyncSession, job: OperatorJob, request: dict) -> dict[str, Any]:
    return {"reports": await reads.recent_reports(db, limit=request.get("limit", 20))}
