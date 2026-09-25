"""
The operator control plane's HTTP surface.

Every route here is authenticated as an operator (the machine credential or a
signed-in admin), rate-limited, and confined to the allowlist in
`services/operator/registry.py`. Free reads answer directly; anything that can
spend money becomes a durable job.

There is no route that takes a command, a query, a path or code.
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.operator_job import OperatorJob
from app.services.batch_job import NoBankedIndex
from app.services.operator import reads
from app.services.operator import truth as truth_service
from app.services.operator.truth import DEFAULT_LABEL
from app.services.operator.auth import (
    OperatorPrincipal, check_paid_rate_limit, operator_request,
)
from app.services.operator.jobs import DuplicateRequest, enqueue, job_to_dict
from app.services.operator.registry import (
    BENCHMARK_CONFIGS, CONFIGS_REQUIRING_ACK, describe,
)
from app.services.operator.sanitize import sanitize
from app.utils.default_user import parse_uuid

router = APIRouter(prefix="/api/operator", tags=["operator"])


async def _report(db: AsyncSession, report_id: str):
    try:
        return await reads.get_report(db, parse_uuid(report_id, "report_id"))
    except LookupError:
        raise HTTPException(status_code=404, detail="Report not found")


# ── Catalogue ───────────────────────────────────────────────────────────

@router.get("/operations", response_model=list[dict[str, Any]])
async def list_operations(principal: OperatorPrincipal = Depends(operator_request)):
    """Exactly what this credential can do, with each operation's cost policy.

    Published so an operator can see the ceiling before spending, and so the
    allowlist is discoverable rather than folklore."""
    return describe()


@router.get("/whoami", response_model=dict[str, Any])
async def whoami(principal: OperatorPrincipal = Depends(operator_request)):
    """Confirms a credential works. Returns the principal's audit name — never
    the credential, nor any part of it."""
    return {"principal": principal.audit_name, "kind": principal.kind}


# ── Free reads ──────────────────────────────────────────────────────────

@router.get("/reports", response_model=list[dict[str, Any]])
async def operator_reports(
    limit: int = Query(default=20, ge=1, le=100),
    principal: OperatorPrincipal = Depends(operator_request),
    db: AsyncSession = Depends(get_db),
):
    """Recent reports and their extraction state. Free."""
    return sanitize(await reads.recent_reports(db, limit=limit))


@router.get("/reports/{report_id}/batch-plan", response_model=dict[str, Any])
async def operator_batch_plan(
    report_id: str,
    batch_size: int = Query(default=4, ge=1, le=8),
    context_pages: int = Query(default=1, ge=0, le=3),
    principal: OperatorPrincipal = Depends(operator_request),
    db: AsyncSession = Depends(get_db),
):
    """The batches this report's banked index produces. Free and read-only."""
    report = await _report(db, report_id)
    try:
        return sanitize(reads.batch_plan(report, batch_size=batch_size,
                                         context_pages=context_pages))
    except (NoBankedIndex, KeyError):
        raise HTTPException(
            status_code=409,
            detail="This report has no banked Stage-1 index; run the index pass first.",
        )


@router.get("/reports/{report_id}/checkpoint", response_model=dict[str, Any])
async def operator_checkpoint(
    report_id: str,
    principal: OperatorPrincipal = Depends(operator_request),
    db: AsyncSession = Depends(get_db),
):
    """What each checkpoint holds, by shape rather than contents. Free."""
    return sanitize(reads.inspect_checkpoint(await _report(db, report_id)))


@router.get("/reports/{report_id}/diagnosis", response_model=dict[str, Any])
async def operator_diagnosis(
    report_id: str,
    principal: OperatorPrincipal = Depends(operator_request),
    db: AsyncSession = Depends(get_db),
):
    """Processing history, the classified failure and attributable AI spend. Free."""
    return sanitize(await reads.diagnose(db, await _report(db, report_id)))


# ── Jobs ────────────────────────────────────────────────────────────────

class TruthAccount(BaseModel):
    """One account's confirmed values, as the document prints them.

    Extra keys are allowed: the scorer only measures the fields a truth entry
    states, so a truth set can start small and grow. Values must be the FULL
    printed field — Experian's Status reads "Voluntarily surrendered. $7,684
    past due as of Sep 2026.", and a truth holding only the first clause
    scores a correct extraction as a miss. Comparison stays exact on purpose:
    accepting a superset would also accept an invented one."""

    model_config = {"extra": "allow"}

    creditor_name: str
    account_number: str | None = None


class BenchmarkTruth(BaseModel):
    accounts: list[TruthAccount] = Field(min_length=1)


class BenchmarkBatchRequest(BaseModel):
    report_id: str
    batch_id: str = "b0"
    # One config per job. There is no "all", and no list form.
    config: Literal["A", "B", "C"]
    # Truth by REFERENCE by default: it is real account data, so it is stored
    # server-side once and selected by label rather than re-entered. Inline
    # truth stays available for the CLI, where a file is the natural source.
    truth: BenchmarkTruth | None = None
    truth_label: str = Field(default=DEFAULT_LABEL, max_length=100)
    batch_size: int = Field(default=4, ge=1, le=8)
    context_pages: int = Field(default=1, ge=0, le=3)
    idempotency_key: str | None = Field(default=None, max_length=200)
    # Sol is the expensive escalation target; asking for it must be deliberate.
    acknowledge_expensive: bool = False


@router.post("/jobs/benchmark-batch", response_model=dict[str, Any], status_code=202)
async def queue_benchmark_batch(
    body: BenchmarkBatchRequest,
    principal: OperatorPrincipal = Depends(operator_request),
    db: AsyncSession = Depends(get_db),
):
    """Queue ONE benchmark of ONE batch under ONE model.

    Returns 202 and a job id; the worker runs it. Exactly one provider call is
    permitted, the models are fixed per config, and nothing is banked."""
    model, _ = BENCHMARK_CONFIGS[body.config]
    if body.config in CONFIGS_REQUIRING_ACK and not body.acknowledge_expensive:
        raise HTTPException(
            status_code=400,
            detail=(f"Config {body.config} uses {model}, the expensive escalation model. "
                    "Re-send with acknowledge_expensive=true to confirm."),
        )

    report = await _report(db, body.report_id)
    # Fail fast on a batch that does not exist, before a job is created and
    # before the paid budget is spent.
    try:
        plans = reads.batch_plans(report, batch_size=body.batch_size,
                                  context_pages=body.context_pages)
    except NoBankedIndex:
        raise HTTPException(
            status_code=409,
            detail="This report has no banked Stage-1 index; run the index pass first.",
        )
    if body.batch_id not in {p.batch_id for p in plans}:
        raise HTTPException(
            status_code=404,
            detail=f"No batch {body.batch_id!r}; this report has {[p.batch_id for p in plans]}",
        )

    # Resolve the truth BEFORE queueing, so the job records which truth it is
    # scored against and a later correction cannot silently answer a different
    # question under a reused idempotency key.
    truth_fingerprint = None
    if body.truth is not None:
        accounts = {"accounts": [a.model_dump() for a in body.truth.accounts]}
        try:
            truth_service.validate(accounts)
        except truth_service.TruthError as e:
            raise HTTPException(status_code=422, detail=str(e))
        truth_fingerprint = truth_service.fingerprint(accounts)
    else:
        try:
            accounts = await truth_service.resolve_for_job(
                db, report.id, body.batch_id, body.truth_label)
        except truth_service.TruthUnverified as e:
            raise HTTPException(status_code=409, detail=str(e))
        except truth_service.TruthError as e:
            raise HTTPException(status_code=404, detail=str(e))
        truth_fingerprint = truth_service.fingerprint(accounts)

    check_paid_rate_limit(principal)

    request = body.model_dump(exclude={"idempotency_key"})
    # The job stores the reference and the fingerprint, not a second copy of
    # the account values — there is one authoritative copy, in the truth store.
    request.pop("truth", None)
    request["truth_fingerprint"] = truth_fingerprint
    request["truth_inline"] = body.truth is not None
    if body.truth is not None:
        # The CLI path has no stored row to resolve later, so its truth does
        # travel with the job.
        request["truth"] = accounts
    try:
        job, created = await enqueue(
            db, operation="benchmark_batch", request=request,
            requested_by=principal.audit_name, report_id=report.id,
            idempotency_key=body.idempotency_key,
        )
    except DuplicateRequest as e:
        raise HTTPException(status_code=409, detail=str(e))
    await db.commit()

    return {
        **job_to_dict(job),
        # False means this key already bought this work: nothing new will be
        # spent and the original job's result is what to read.
        "created": created,
        "model": model,
        "max_model_calls": job.max_model_calls,
    }


# ── Benchmark truth ─────────────────────────────────────────────────────

class TruthUpsert(BaseModel):
    accounts: list[TruthAccount] = Field(min_length=1)
    label: str = Field(default=DEFAULT_LABEL, max_length=100)
    # Confirming the values against the document is a separate, deliberate act
    # from saving them.
    verified: bool = False
    note: str | None = Field(default=None, max_length=2000)


@router.get("/reports/{report_id}/truth", response_model=list[dict[str, Any]])
async def list_truth(
    report_id: str,
    principal: OperatorPrincipal = Depends(operator_request),
    db: AsyncSession = Depends(get_db),
):
    """Which batches have stored truth, and whether it is usable.

    Summaries only — counts and status, no account values — so a report
    overview carries none of the data it is describing."""
    report = await _report(db, report_id)
    rows = await truth_service.list_for_report(db, report.id)
    return [truth_service.summary(row) for row in rows]


@router.get("/reports/{report_id}/batches/{batch_id}/truth", response_model=dict[str, Any])
async def get_truth(
    report_id: str, batch_id: str, label: str = DEFAULT_LABEL,
    principal: OperatorPrincipal = Depends(operator_request),
    db: AsyncSession = Depends(get_db),
):
    """The stored truth for one batch, for review or correction."""
    report = await _report(db, report_id)
    row = await truth_service.get(db, report.id, batch_id, label)
    if row is None:
        raise HTTPException(status_code=404, detail="No truth stored for this batch")
    return truth_service.to_dict(row)


@router.put("/reports/{report_id}/batches/{batch_id}/truth", response_model=dict[str, Any])
async def put_truth(
    report_id: str, batch_id: str, body: TruthUpsert,
    principal: OperatorPrincipal = Depends(operator_request),
    db: AsyncSession = Depends(get_db),
):
    """Store or correct the truth for one batch.

    Saved once, then referenced. Correcting it resets `verified`, because the
    confirmation was of the previous values."""
    report = await _report(db, report_id)
    try:
        row = await truth_service.upsert(
            db, report_id=report.id, batch_id=batch_id,
            accounts={"accounts": [a.model_dump() for a in body.accounts]},
            created_by=principal.audit_name, label=body.label,
            verified=body.verified, source="operator", note=body.note,
        )
    except truth_service.TruthError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await db.commit()
    return truth_service.to_dict(row)


@router.post("/reports/{report_id}/batches/{batch_id}/truth/verify",
             response_model=dict[str, Any])
async def verify_truth(
    report_id: str, batch_id: str, label: str = DEFAULT_LABEL, verified: bool = True,
    principal: OperatorPrincipal = Depends(operator_request),
    db: AsyncSession = Depends(get_db),
):
    """Record that a human has checked this truth against the document.

    A separate act from saving it, and the thing a benchmark requires."""
    report = await _report(db, report_id)
    row = await truth_service.get(db, report.id, batch_id, label)
    if row is None:
        raise HTTPException(status_code=404, detail="No truth stored for this batch")
    await truth_service.set_verified(db, row, verified)
    await db.commit()
    return truth_service.to_dict(row)


@router.post("/reports/{report_id}/batches/{batch_id}/truth/draft",
             response_model=dict[str, Any])
async def draft_truth(
    report_id: str, batch_id: str, label: str = DEFAULT_LABEL,
    principal: OperatorPrincipal = Depends(operator_request),
    db: AsyncSession = Depends(get_db),
):
    """Prefill a truth DRAFT from this batch's banked extraction.

    An ergonomic head start — the accounts and fields to correct, instead of
    typing them out on a phone. It is stored UNVERIFIED and the benchmark will
    refuse it: scoring a model against its own output measures self-consistency
    and nothing else. A human must correct it against the document and verify
    it first."""
    report = await _report(db, report_id)
    banked = ((report.extraction_checkpoint or {}).get("batches") or {}).get(batch_id)
    if not banked:
        raise HTTPException(
            status_code=409,
            detail=f"Batch {batch_id} has no banked extraction to draft from.",
        )
    try:
        drafted = truth_service.draft_from_batch(banked)
        row = await truth_service.upsert(
            db, report_id=report.id, batch_id=batch_id, accounts=drafted,
            created_by=principal.audit_name, label=label, verified=False,
            source="drafted_from_batch",
            note="Drafted from the banked extraction. Correct against the document, "
                 "then verify. Not truth until then.",
        )
    except truth_service.TruthError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await db.commit()
    return {**truth_service.to_dict(row),
            "warning": "Drafted from a model's own output. Correct it against the document "
                       "and verify it; a benchmark against unverified truth is refused."}


@router.get("/jobs/{job_id}", response_model=dict[str, Any])
async def get_job(
    job_id: str,
    principal: OperatorPrincipal = Depends(operator_request),
    db: AsyncSession = Depends(get_db),
):
    job = await db.get(OperatorJob, parse_uuid(job_id, "job_id"))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job_to_dict(job)


@router.get("/jobs", response_model=list[dict[str, Any]])
async def list_jobs(
    report_id: str | None = None,
    operation: str | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    principal: OperatorPrincipal = Depends(operator_request),
    db: AsyncSession = Depends(get_db),
):
    """Recent operator jobs, newest first."""
    query = select(OperatorJob).order_by(OperatorJob.created_at.desc()).limit(limit)
    if report_id:
        query = query.where(OperatorJob.report_id == parse_uuid(report_id, "report_id"))
    if operation:
        query = query.where(OperatorJob.operation == operation)
    rows = (await db.execute(query)).scalars().all()
    return [job_to_dict(job) for job in rows]
