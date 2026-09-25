"""
Durability of the operator queue.

The control plane's promise is that a request survives the process: an
operator on a phone can close the tab, the container can restart, and the job
is still there with exactly one execution behind it.

The subtle rule is what happens to a job whose worker died mid-flight. A FREE
operation is requeued. A PAID one is failed, because the provider call may
already have happened and been billed, and quietly re-running it would buy the
same work twice. That asymmetry is the whole recovery policy and is asserted
here rather than described in a comment.
"""
import asyncio
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models.operator_job import JobStatus, OperatorJob
from app.services.operator.jobs import (
    DuplicateRequest, claim_next, enqueue, fingerprint, recover_stale, run_job,
)
from app.services.operator.registry import OPERATIONS, get_operation
from app.services.operator.sanitize import REDACTED, safe_error, sanitize
from tests.conftest import requires_db

BACKEND = Path(__file__).resolve().parent.parent / "backend"


# ── Sanitization (no database needed) ───────────────────────────────────

def test_the_sanitizer_strips_identity_documents_and_credentials():
    payload = {
        "creditor_name": "CAINE & WEINER",          # allowed
        "account_number": "88XXXX2211",             # allowed, masked
        "source_pages": [7, 8],                     # allowed
        "personal_info": {"name": "JANE Q CONSUMER"},
        "address": "88 Birch Lane",
        "ssn_last_four": "4321",
        "raw_text": "JANE Q CONSUMER 987-65-4321",
        "storage_key": "users/abc/report.pdf",
        "openai_api_key": "sk-abcdefghijklmnopqrstuvwxyz",
        "nested": {"contact": {"phone": "503-555-0177"}, "balance": "$1,204"},
    }
    clean = sanitize(payload)

    assert clean["creditor_name"] == "CAINE & WEINER"
    assert clean["account_number"] == "88XXXX2211"
    assert clean["source_pages"] == [7, 8]
    assert clean["nested"]["balance"] == "$1,204"
    for redacted in ("personal_info", "address", "ssn_last_four", "raw_text",
                     "storage_key", "openai_api_key"):
        assert clean[redacted] == REDACTED, redacted
    assert clean["nested"]["contact"] == REDACTED


def test_the_sanitizer_catches_secrets_and_documents_by_value_not_only_by_key():
    """A key nobody thought to name is the one that leaks."""
    clean = sanitize({
        "surprise": "%PDF-1.4 binary content here",
        "note": "the key is sk-livekey1234567890abcdef",
        "ssn_in_prose": "his number is 987-65-4321 apparently",
        "inline": "data:application/pdf;base64,JVBERi0=",
        "blob": b"%PDF-1.4",
    })
    assert clean["surprise"] == REDACTED
    assert clean["inline"] == REDACTED
    assert clean["blob"] == REDACTED
    assert "sk-livekey1234567890abcdef" not in clean["note"]
    assert "987-65-4321" not in clean["ssn_in_prose"]


def test_unknown_failures_get_a_generic_message_rather_than_their_own():
    """An unvetted message is exactly the one likely to carry a provider's
    wording, a file path or part of the request."""
    known, message = safe_error(RuntimeError("connect to 10.0.0.4:5432 failed for user admin"))
    assert known == "RuntimeError"
    assert "10.0.0.4" not in message
    assert message == "The operation failed. See the server log for details."

    name, text = safe_error("AIResponseError")
    assert name == "AIResponseError" and "billed" in text


def test_every_allowlisted_operation_declares_a_cost_policy():
    for name, op in OPERATIONS.items():
        assert op.name == name
        assert op.summary
        if op.paid:
            assert op.max_model_calls >= 1
            assert op.models, "a paid operation must name the models it may reach"
            assert not op.resumable, "a paid job must not be auto-retried after a crash"
        else:
            assert op.max_model_calls == 0
            assert op.resumable


def test_the_only_paid_operation_is_the_benchmark_and_it_allows_one_call():
    paid = {name for name, op in OPERATIONS.items() if op.paid}
    assert paid == {"benchmark_batch"}
    assert get_operation("benchmark_batch").max_model_calls == 1


def test_an_operation_outside_the_allowlist_cannot_be_named():
    from app.services.operator.registry import UnknownOperation

    for attempt in ("run_shell", "eval", "__import__", "os.system", "retry_extraction"):
        with pytest.raises(UnknownOperation) as caught:
            get_operation(attempt)
        # The error lists what is allowed rather than echoing the attempt, so
        # the endpoint cannot be used to probe for hidden names.
        assert attempt not in str(caught.value)


def test_the_fingerprint_distinguishes_materially_different_requests():
    base = {"report_id": "r", "batch_id": "b0", "config": "A",
            "truth": {"accounts": [{"creditor_name": "X"}]}}
    assert fingerprint(base) == fingerprint(dict(reversed(list(base.items()))))
    assert fingerprint(base) != fingerprint({**base, "config": "B"})
    # Corrected truth is a different question and must not reuse a job.
    corrected = {**base, "truth": {"accounts": [{"creditor_name": "X", "status_raw": "full"}]}}
    assert fingerprint(base) != fingerprint(corrected)


def test_importing_the_app_starts_no_operator_worker():
    """The worker runs because main starts it under a setting, never as a
    side effect of an import — otherwise a migration or a shell would begin
    executing paid jobs."""
    probe = (
        "import asyncio, app.main; "
        "print(len([t for t in asyncio.all_tasks(asyncio.new_event_loop())]))"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=BACKEND,
                            capture_output=True, text=True, timeout=120,
                            env={**__import__("os").environ, "DB_NULL_POOL": "1"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0"


# ── Queue behaviour ─────────────────────────────────────────────────────

pytestmark_db = requires_db


@requires_db
class TestQueue:
    @staticmethod
    async def _enqueue(operation="diagnose_report", request=None, **kwargs):
        from app.database import async_session_maker

        async with async_session_maker() as db:
            job, created = await enqueue(
                db, operation=operation, request=request or {"report_id": str(uuid.uuid4())},
                requested_by=kwargs.pop("requested_by", "agent:test"), **kwargs,
            )
            await db.commit()
            return job.id, created

    @staticmethod
    async def _get(job_id) -> OperatorJob:
        from app.database import async_session_maker

        async with async_session_maker() as db:
            return await db.get(OperatorJob, job_id)

    async def test_a_queued_job_survives_the_process(self, db_ready):
        """Durability is the row, not the request that created it."""
        job_id, created = await self._enqueue()
        assert created
        job = await self._get(job_id)
        assert job.status == JobStatus.QUEUED
        assert job.attempt_count == 0
        assert job.requested_by == "agent:test"

    async def test_claiming_is_atomic_so_one_job_runs_once(self, db_ready):
        """Two workers racing for the same row is the failure that would
        double-charge, so the claim is a single locking statement."""
        ids = [await self._enqueue() for _ in range(3)]
        claimed = await asyncio.gather(*[claim_next() for _ in range(6)])
        taken = [c for c in claimed if c is not None]

        assert len(taken) == 3, "a job was claimed twice or not at all"
        assert len(set(taken)) == 3
        assert {str(t) for t in taken} == {str(i) for i, _ in ids}
        for job_id, _ in ids:
            job = await self._get(job_id)
            assert job.status == JobStatus.RUNNING
            assert job.attempt_count == 1
            assert job.started_at is not None

    async def test_claiming_an_empty_queue_returns_nothing(self, db_ready):
        assert await claim_next() is None

    async def test_the_same_key_and_request_returns_the_original_job(self, db_ready):
        request = {"report_id": str(uuid.uuid4()), "config": "A"}
        first, created_first = await self._enqueue(request=request, idempotency_key="k1")
        second, created_second = await self._enqueue(request=request, idempotency_key="k1")
        assert created_first is True and created_second is False
        assert first == second

    async def test_the_same_key_with_a_different_request_is_refused(self, db_ready):
        from app.database import async_session_maker

        await self._enqueue(request={"report_id": "a"}, idempotency_key="k2")
        async with async_session_maker() as db:
            with pytest.raises(DuplicateRequest):
                await enqueue(db, operation="diagnose_report", request={"report_id": "b"},
                              requested_by="agent:test", idempotency_key="k2")

    async def test_one_key_per_principal_not_globally(self, db_ready):
        """Two operators may each use "b0-luna" without one silently
        receiving the other's job."""
        request = {"report_id": str(uuid.uuid4())}
        mine, _ = await self._enqueue(request=request, idempotency_key="b0",
                                      requested_by="agent:codex")
        theirs, created = await self._enqueue(request=request, idempotency_key="b0",
                                              requested_by="user:someone")
        assert created is True
        assert mine != theirs

    # ── Recovery after an interrupted worker ────────────────────────────

    @staticmethod
    async def _make_stale(job_id):
        from app.database import async_session_maker

        async with async_session_maker() as db:
            job = await db.get(OperatorJob, job_id)
            job.status = JobStatus.RUNNING
            job.started_at = datetime.now(timezone.utc) - timedelta(hours=2)
            await db.commit()

    async def test_a_free_job_is_requeued_after_an_interrupted_worker(self, db_ready):
        job_id, _ = await self._enqueue(operation="diagnose_report")
        await self._make_stale(job_id)

        assert await recover_stale() == 1
        job = await self._get(job_id)
        assert job.status == JobStatus.QUEUED
        assert job.started_at is None
        # And it can be picked up again.
        assert await claim_next() == job_id

    async def test_a_paid_job_is_failed_rather_than_silently_re_run(self, db_ready):
        """The provider call may already have happened and been billed.
        Re-running it would buy the same work twice, so the operator decides."""
        job_id, _ = await self._enqueue(
            operation="benchmark_batch",
            request={"report_id": str(uuid.uuid4()), "batch_id": "b0", "config": "A",
                     "truth": {"accounts": [{"creditor_name": "X"}]}},
        )
        await self._make_stale(job_id)

        assert await recover_stale() == 1
        job = await self._get(job_id)
        assert job.status == JobStatus.FAILED
        assert job.safe_error_class == "WorkerInterrupted"
        assert "not retried automatically" in job.safe_error_message
        assert await claim_next() is None, "a paid job was requeued"

    async def test_a_job_still_within_its_lease_is_left_alone(self, db_ready):
        from app.database import async_session_maker

        job_id, _ = await self._enqueue()
        async with async_session_maker() as db:
            job = await db.get(OperatorJob, job_id)
            job.status = JobStatus.RUNNING
            job.started_at = datetime.now(timezone.utc)
            await db.commit()

        assert await recover_stale() == 0
        assert (await self._get(job_id)).status == JobStatus.RUNNING

    async def test_a_handler_failure_records_a_safe_error_and_no_result(self, db_ready):
        # diagnose_report against a report that does not exist.
        job_id, _ = await self._enqueue(request={"report_id": str(uuid.uuid4())})
        await claim_next()
        assert await run_job(job_id) == JobStatus.FAILED

        job = await self._get(job_id)
        assert job.safe_error_class == "LookupError"
        assert job.safe_error_message == ("The requested report, batch or record does not exist.")
        assert job.result_json is None
        assert job.finished_at is not None
        assert job.model_calls_made == 0
        assert job.estimated_cost_usd is None

    async def test_running_a_job_that_was_never_claimed_is_a_no_op(self, db_ready):
        """run_job only executes a job the claim statement already flipped to
        running, so a stray call cannot start work out of band."""
        job_id, _ = await self._enqueue()
        assert await run_job(job_id) == JobStatus.QUEUED
        assert (await self._get(job_id)).status == JobStatus.QUEUED
