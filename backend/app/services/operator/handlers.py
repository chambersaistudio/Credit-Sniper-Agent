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
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.credit_report import CreditReport
from app.models.operator_job import OperatorJob
from app.services.batch_job import batch_plans, extract_batch
from app.services.benchmark.batch_scoring import score_batch
from app.services.operator import reads
from app.services.operator.jobs import handler
from app.services.operator.registry import config_model
from app.services.storage import get_storage

logger = logging.getLogger(__name__)


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
    truth_accounts = (request.get("truth") or {}).get("accounts") or []
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

    # Re-read the row to prove nothing was written by this run.
    await db.refresh(report)
    banked_after = _banked_digest(report)

    return {
        "report_id": str(report.id),
        "batch_id": batch_id,
        "config": config,
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
