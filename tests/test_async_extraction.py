"""
Extraction as a durable background job.

The live blocker this covers: reading an Experian disclosure took longer than
the browser held the connection, so the consumer saw a network failure for a
pass we had already paid for. Upload now returns 202 and a report id, and
every expensive pass is checkpointed the instant it succeeds.

The promises asserted here:
  * upload returns immediately; nothing expensive happens in the request
  * a failed audit never re-runs the extractor
  * a failed persistence never re-runs either model
  * the same bytes never start a second billable job
  * a job interrupted mid-pass resumes instead of restarting
  * provider internals never reach a consumer-facing response
"""
import pytest

from app.services.ai import AIProviderError
from app.services.document_extraction.schema import AuditReport, CreditReportExtraction
from tests.conftest import assert_no_provider_internals, drain_extraction_queue, requires_db
from tests.test_document_ingestion_flow import (  # reuse the golden Experian fixtures
    SOURCE_PDF_TEXT, FakeDocumentProvider, _pdf, client, document_ai, golden_extraction,
    verifying_audit,
)

pytestmark = requires_db

# What the live OpenAI outage actually said. None of it may reach a consumer.
QUOTA_ERROR = (
    "OpenAI API error 429: You exceeded your current quota, please check your plan "
    "and billing details. (insufficient_quota / credit_balance_exhausted)"
)


class ScriptedProvider(FakeDocumentProvider):
    """A document provider that can fail a chosen pass, and records which pass
    it was asked for each time."""

    def __init__(self, extraction, audit, *, fail_extraction=0, fail_audit=0):
        super().__init__(extraction, audit)
        self.fail_extraction = fail_extraction
        self.fail_audit = fail_audit
        self.passes: list[str] = []

    async def generate_document(self, config, *, output_type, **kwargs):
        which = "extract" if output_type is CreditReportExtraction else "audit"
        self.passes.append(which)
        if which == "extract" and self.fail_extraction:
            self.fail_extraction -= 1
            raise AIProviderError(QUOTA_ERROR)
        if which == "audit" and self.fail_audit:
            self.fail_audit -= 1
            raise AIProviderError(QUOTA_ERROR)
        return await super().generate_document(config, output_type=output_type, **kwargs)


def _scripted(**kwargs) -> ScriptedProvider:
    from app.services.ai import register_provider

    provider = ScriptedProvider(golden_extraction(), verifying_audit(), **kwargs)
    register_provider("openai", provider)
    return provider


# Built once: reportlab stamps a creation time, so re-rendering would give
# different bytes and idempotency is defined on the bytes.
PDF_BYTES = _pdf(SOURCE_PDF_TEXT)


async def _post(client, bureau="auto_detect"):
    return await client.post(
        "/api/reports/upload", files={"file": ("r.pdf", PDF_BYTES, "application/pdf")},
        data={"bureau": bureau},
    )


async def _row(report_id):
    """The stored report, read outside the API so operator-only fields are
    visible to the test."""
    from app.database import async_session_maker
    from app.models.credit_report import CreditReport

    async with async_session_maker() as db:
        return await db.get(CreditReport, report_id)


async def test_upload_returns_immediately_without_reading_the_document(client, document_ai):
    provider = _scripted()
    response = await _post(client)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["processing_stage"] == "queued"
    assert body["processing"] is True
    assert body["duplicate"] is False
    assert body["status_url"] == f"/api/reports/{body['report_id']}/status"
    # The expensive part has not started, so no long-lived HTTP request was
    # ever required for it.
    assert provider.passes == []

    status = (await client.get(body["status_url"])).json()
    assert (status["processing_stage"], status["processing"]) == ("queued", True)
    assert status["attempt_count"] == 0

    await drain_extraction_queue()
    status = (await client.get(body["status_url"])).json()
    assert (status["processing_stage"], status["processing"]) == ("verified", False)
    assert status["extraction_status"] == "verified"
    assert status["processing_started_at"] and status["processing_finished_at"]
    assert provider.passes == ["extract", "audit"]


async def test_stage_is_visible_while_each_pass_runs(client, document_ai):
    """Each stage is persisted before its pass, so a polling client can say
    what is happening rather than showing an unexplained spinner."""
    seen: list[tuple[str, str]] = []

    class Observing(ScriptedProvider):
        async def generate_document(self, config, *, output_type, **kwargs):
            which = "extract" if output_type is CreditReportExtraction else "audit"
            report = await _row(report_id)
            seen.append((which, report.processing_stage))
            return await super().generate_document(config, output_type=output_type, **kwargs)

    from app.services.ai import register_provider

    register_provider("openai", Observing(golden_extraction(), verifying_audit()))
    report_id = (await _post(client)).json()["report_id"]
    await drain_extraction_queue()

    assert seen == [("extract", "extracting"), ("audit", "auditing")]
    row = await _row(report_id)
    # The terminal stage IS the quality state, so the two can never disagree.
    assert row.processing_stage == row.extraction_status == "verified"


async def test_failed_audit_never_re_runs_the_extractor(client, document_ai):
    """The rule that makes retries affordable: extraction succeeded, so it is
    not paid for twice just because the second pass failed."""
    provider = _scripted(fail_audit=1)
    report_id = (await _post(client)).json()["report_id"]
    await drain_extraction_queue()

    assert provider.passes == ["extract", "audit"]
    row = await _row(report_id)
    # The extraction is banked, and the audit is recorded as having failed.
    assert row.extraction_checkpoint["extraction"]["accounts"]
    assert row.extraction_checkpoint["audit"] is None
    assert QUOTA_ERROR in row.extraction_checkpoint["audit_error"]
    # Read fine, unverified: held out of dispute analysis, not called failed.
    assert row.extraction_status == "needs_audit"
    assert row.processing_stage == "needs_audit"

    # Re-queue. Only the pass that failed is bought again.
    from app.services.extraction_jobs import enqueue

    from app.database import async_session_maker

    async with async_session_maker() as db:
        row = await db.get(type(row), report_id)
        enqueue(row, reset_audit=True)
        await db.commit()
    await drain_extraction_queue()

    assert provider.passes == ["extract", "audit", "audit"], "the extractor must not run again"
    row = await _row(report_id)
    assert row.extraction_status == "verified"
    assert row.attempt_count == 2


async def test_failed_persistence_never_re_runs_either_model(client, document_ai, monkeypatch):
    """Both passes succeeded and are on the row. A crash while storing the
    result costs the database write again — never the two model calls."""
    provider = _scripted()
    from app.services import extraction_jobs

    real_persist = extraction_jobs.persist_outcome
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database went away")
        return await real_persist(*args, **kwargs)

    monkeypatch.setattr(extraction_jobs, "persist_outcome", flaky)

    report_id = (await _post(client)).json()["report_id"]
    await drain_extraction_queue()

    assert provider.passes == ["extract", "audit"]
    row = await _row(report_id)
    assert row.processing_stage == "failed"
    # Operator-only: the exception class is kept for us, not for the consumer.
    assert row.last_processing_error_class == "RuntimeError"
    assert row.extraction_checkpoint["extraction"] and row.extraction_checkpoint["audit"]

    # FAILED is retryable, so the ordinary consumer-facing retry finishes it.
    retried = await client.post(f"/api/reports/{report_id}/retry-extraction")
    assert retried.status_code == 202, retried.text
    await drain_extraction_queue()

    assert provider.passes == ["extract", "audit"], "neither model may be called a second time"
    detail = (await client.get(f"/api/reports/{report_id}")).json()
    assert detail["extraction_status"] == "verified"
    assert detail["total_accounts"] == 15


async def test_interrupted_job_resumes_from_its_checkpoint(client, document_ai):
    """A restart mid-pass leaves the row in a running stage. The worker picks
    it up again rather than abandoning it."""
    provider = _scripted()
    report_id = (await _post(client)).json()["report_id"]

    from app.database import async_session_maker
    from app.models.credit_report import CreditReport

    # As if the process died between storing the extraction and auditing it.
    async with async_session_maker() as db:
        row = await db.get(CreditReport, report_id)
        row.processing_stage = "extracting"
        row.extraction_checkpoint = {
            "extraction": golden_extraction().model_dump(mode="json"),
            "extractor_model": "gpt-5.6-sol",
        }
        await db.commit()

    await drain_extraction_queue()
    assert provider.passes == ["audit"], "the banked extraction must be reused"
    row = await _row(report_id)
    assert row.extraction_status == "verified"


async def test_identical_bytes_never_start_a_second_billable_job(client, document_ai):
    """Double-clicking Upload, or re-submitting the same file, must not buy a
    second reading of the same document."""
    provider = _scripted()
    first = (await _post(client)).json()
    second = (await _post(client)).json()

    assert second["report_id"] == first["report_id"]
    assert second["duplicate"] is True
    await drain_extraction_queue()
    assert provider.passes == ["extract", "audit"]

    # And a third submission after it finished still resolves to the same
    # report rather than a duplicate row.
    third = (await _post(client)).json()
    assert third["report_id"] == first["report_id"]
    await drain_extraction_queue()
    assert provider.passes == ["extract", "audit"]
    assert len((await client.get("/api/reports/")).json()) == 1


async def test_duplicate_upload_resumes_an_outage_rather_than_duplicating_it(client, document_ai):
    """The provider was down, so nothing was read. Re-uploading the same file
    resumes that report instead of creating a second one."""
    provider = _scripted(fail_extraction=1)
    first = (await _post(client)).json()
    await drain_extraction_queue()
    assert (await _row(first["report_id"])).extraction_status == "provider_unavailable"

    again = (await _post(client)).json()
    assert again["report_id"] == first["report_id"]
    assert again["duplicate"] is True
    assert again["processing"] is True
    await drain_extraction_queue()

    assert provider.passes == ["extract", "extract", "audit"]
    detail = (await client.get(f"/api/reports/{first['report_id']}")).json()
    assert detail["extraction_status"] == "verified"
    assert len((await client.get("/api/reports/")).json()) == 1


async def test_processing_responses_carry_no_provider_internals(client, document_ai):
    provider = _scripted(fail_extraction=1)
    report_id = (await _post(client)).json()["report_id"]
    await drain_extraction_queue()

    status = await client.get(f"/api/reports/{report_id}/status")
    detail = await client.get(f"/api/reports/{report_id}")
    listing = await client.get("/api/reports/")
    for response in (status, detail, listing):
        assert_no_provider_internals(response.text)

    # Operators still get it, on the row.
    row = await _row(report_id)
    assert row.last_processing_error_class == "AIProviderError"
    assert provider.passes == ["extract"]


async def test_retry_while_in_flight_is_idempotent(client, document_ai):
    _scripted()
    report_id = (await _post(client)).json()["report_id"]
    # Still queued: clicking retry must not stack a second job or 409.
    retried = await client.post(f"/api/reports/{report_id}/retry-extraction")
    assert retried.status_code == 202
    assert retried.json()["duplicate"] is True
    assert len(await drain_extraction_queue()) == 1


async def test_upload_still_rejects_what_it_can_check_synchronously(client, document_ai):
    _scripted()
    not_pdf = await client.post(
        "/api/reports/upload", files={"file": ("r.pdf", b"hello", "application/pdf")}
    )
    assert not_pdf.status_code == 400
    tri_merge = await _post(client, bureau="tri_merge")
    assert tri_merge.status_code == 422
    nonsense = await _post(client, bureau="equifacts")
    assert nonsense.status_code == 422
