"""
The durable operator job queue and its worker.

Durability is the database, not the process. A request writes a row and
returns; a worker inside the Railway backend claims it with
`FOR UPDATE SKIP LOCKED` — so two workers can never take the same row — runs
the allowlisted handler, and writes the result back. Nothing depends on the
request that created the job still being alive, which is the whole point: an
operator on a phone can close the tab.

Three rules the queue enforces rather than documents:

* **One idempotency key buys one job.** A repeat of the same key with the same
  request returns the original row. With a *different* request it is a
  conflict, never a silent re-purchase under a reused name.
* **A paid job interrupted mid-flight is failed, not retried.** The worker
  died after the provider call may have already happened and been billed;
  re-running it would buy the same work again. The operator decides.
* **The model-call ceiling is counted, not trusted.** The runner meters
  actual provider calls and fails the job if a handler exceeds what its
  registry entry allowed, or reaches a model that entry does not list.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session_maker
from app.models.operator_job import JobStatus, OperatorJob
from app.services.ai import UsageRecord, only_usage_listener
from app.services.operator.registry import Operation, get_operation
from app.services.operator.sanitize import safe_error, sanitize

logger = logging.getLogger(__name__)

# operation name -> handler(db, job, request) -> result dict
Handler = Callable[[AsyncSession, OperatorJob, dict], Awaitable[dict[str, Any]]]
_HANDLERS: dict[str, Handler] = {}


def handler(operation: str):
    """Register the implementation of an allowlisted operation."""
    def register(fn: Handler) -> Handler:
        get_operation(operation)  # refuse to register anything not allowlisted
        _HANDLERS[operation] = fn
        return fn
    return register


class DuplicateRequest(Exception):
    """This idempotency key was used for a materially different request."""


class BudgetExceeded(Exception):
    """A handler tried to spend more than its registry entry allows."""


def fingerprint(request: dict[str, Any]) -> str:
    """A stable hash of the request, so "same key, same request" is decidable.

    Benchmark truth is part of the request and therefore part of the
    fingerprint: re-running the same batch against *corrected* truth is a
    different question and must not be answered from the old job."""
    return hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


async def enqueue(
    db: AsyncSession, *, operation: str, request: dict[str, Any], requested_by: str,
    report_id: uuid.UUID | str | None = None, idempotency_key: str | None = None,
) -> tuple[OperatorJob, bool]:
    """Create a job, or return the one this key already bought.

    Returns (job, created). `created=False` means the work was already
    requested and nothing new will be spent."""
    spec = get_operation(operation)
    digest = fingerprint(request)

    if idempotency_key:
        existing = (await db.execute(
            select(OperatorJob).where(
                OperatorJob.requested_by == requested_by,
                OperatorJob.idempotency_key == idempotency_key,
            )
        )).scalar_one_or_none()
        if existing is not None:
            if existing.request_fingerprint != digest:
                raise DuplicateRequest(
                    "this idempotency key was used for a different request; "
                    "use a new key to run different work"
                )
            return existing, False

    job = OperatorJob(
        id=uuid.uuid4(),
        operation=operation,
        status=JobStatus.QUEUED,
        report_id=uuid.UUID(str(report_id)) if report_id else None,
        requested_by=requested_by,
        request_json=sanitize(request),
        max_model_calls=spec.max_model_calls,
        idempotency_key=idempotency_key,
        request_fingerprint=digest,
        created_at=datetime.now(timezone.utc),
    )
    db.add(job)
    await db.flush()
    return job, True


# ── Claiming ────────────────────────────────────────────────────────────

_CLAIM = text("""
    UPDATE operator_jobs
       SET status = 'running',
           started_at = now(),
           attempt_count = attempt_count + 1
     WHERE id = (
           SELECT id FROM operator_jobs
            WHERE status = 'queued'
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
     )
 RETURNING id
""")


async def claim_next(session_factory=None) -> uuid.UUID | None:
    """Atomically take one queued job. SKIP LOCKED makes this safe with any
    number of workers: the row is locked and flipped to running in a single
    statement, so two workers cannot both see it as available."""
    factory = session_factory or async_session_maker
    async with factory() as db:
        claimed = (await db.execute(_CLAIM)).scalar_one_or_none()
        await db.commit()
        return claimed


async def recover_stale(session_factory=None) -> int:
    """Deal with jobs whose worker died while they were running.

    A FREE operation is simply requeued. A PAID one is failed: the provider
    call may already have happened and been billed, and quietly running it
    again would buy the same work twice. Resubmitting is the operator's
    decision to make, not ours."""
    factory = session_factory or async_session_maker
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.operator_job_lease_seconds)
    recovered = 0
    async with factory() as db:
        stale = (await db.execute(
            select(OperatorJob).where(
                OperatorJob.status == JobStatus.RUNNING,
                OperatorJob.started_at < cutoff,
            )
        )).scalars().all()
        for job in stale:
            spec = get_operation(job.operation)
            if spec.resumable:
                job.status = JobStatus.QUEUED
                job.started_at = None
            else:
                job.status = JobStatus.FAILED
                job.finished_at = datetime.now(timezone.utc)
                job.safe_error_class, job.safe_error_message = safe_error("WorkerInterrupted")
            recovered += 1
            logger.warning("Operator job %s (%s) was stale; %s",
                           job.id, job.operation,
                           "requeued" if spec.resumable else "failed (paid, not auto-retried)")
        if recovered:
            await db.commit()
    return recovered


# ── Running ─────────────────────────────────────────────────────────────

class _Meter:
    """Counts provider calls and the models they reached.

    The ceiling in the registry is enforced from here rather than inside each
    handler, so a handler cannot exceed its budget by forgetting to check."""

    def __init__(self, spec: Operation):
        self.spec = spec
        self.records: list[UsageRecord] = []

    async def __call__(self, record: UsageRecord) -> None:
        self.records.append(record)

    @property
    def calls(self) -> int:
        return len(self.records)

    @property
    def cost(self) -> float | None:
        known = [r.estimated_cost_usd for r in self.records if r.estimated_cost_usd is not None]
        return round(sum(known), 6) if known else None

    def verify(self) -> None:
        if self.calls > self.spec.max_model_calls:
            raise BudgetExceeded(
                f"{self.spec.name} is limited to {self.spec.max_model_calls} provider call(s) "
                f"but made {self.calls}"
            )
        # No silent escalation: a job that was authorised for Luna may not
        # have reached Terra or Sol, whatever the handler intended.
        for record in self.records:
            if self.spec.models and record.model not in self.spec.models:
                raise BudgetExceeded(
                    f"{self.spec.name} reached model {record.model!r}, which it is not allowed to use"
                )


async def run_job(job_id, *, session_factory=None) -> str:
    """Execute one claimed job and record a sanitized result."""
    factory = session_factory or async_session_maker
    async with factory() as db:
        job = await db.get(OperatorJob, job_id)
        if job is None:
            return JobStatus.FAILED
        if job.status != JobStatus.RUNNING:
            return job.status

        spec = get_operation(job.operation)
        implementation = _HANDLERS.get(job.operation)
        meter = _Meter(spec)
        try:
            if implementation is None:
                raise LookupError(f"no handler for {job.operation}")
            # Usage goes to the meter alone for the duration, so the job's
            # accounting is exact and the app's listeners are restored after.
            with only_usage_listener(meter):
                result = await implementation(db, job, job.request_json or {})
            meter.verify()
            job.result_json = sanitize(result)
            job.status = JobStatus.SUCCEEDED
            job.safe_error_class = job.safe_error_message = None
        except Exception as e:
            # The real exception goes to the server log; the operator gets a
            # class and a message we wrote.
            logger.exception("Operator job %s (%s) failed", job.id, job.operation)
            job.status = JobStatus.FAILED
            job.safe_error_class, job.safe_error_message = safe_error(e)
            job.result_json = None
        finally:
            job.model_calls_made = meter.calls
            job.estimated_cost_usd = meter.cost
            job.finished_at = datetime.now(timezone.utc)
            await db.commit()
        return job.status


async def worker_loop(poll_seconds: float | None = None) -> None:
    """The in-process operator worker.

    Separate from the extraction worker on purpose: operator work is manual,
    occasional and paid, and must not compete with, or be starved by, the
    queue that serves consumer uploads."""
    delay = poll_seconds or settings.operator_worker_poll_seconds
    logger.info("Operator worker started")
    try:
        await recover_stale()
    except Exception:
        logger.exception("Operator worker could not recover stale jobs")
    while True:
        try:
            job_id = await claim_next()
            if job_id is None:
                await asyncio.sleep(delay)
                continue
            await run_job(job_id)
        except asyncio.CancelledError:
            logger.info("Operator worker stopping")
            raise
        except Exception:
            logger.exception("Operator worker iteration failed")
            await asyncio.sleep(delay)


def job_to_dict(job: OperatorJob) -> dict[str, Any]:
    """The operator-facing view of a job. Sanitized on the way out, even for
    a result stored before a redaction rule existed."""
    return {
        "job_id": str(job.id),
        "operation": job.operation,
        "status": job.status,
        "report_id": str(job.report_id) if job.report_id else None,
        "requested_by": job.requested_by,
        "request": sanitize(job.request_json or {}),
        "result": sanitize(job.result_json) if job.result_json else None,
        "error": (
            {"class": job.safe_error_class, "message": job.safe_error_message}
            if job.safe_error_class else None
        ),
        "estimated_cost_usd": job.estimated_cost_usd,
        "max_model_calls": job.max_model_calls,
        "model_calls_made": job.model_calls_made,
        "attempt_count": job.attempt_count,
        "idempotency_key": job.idempotency_key,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }
