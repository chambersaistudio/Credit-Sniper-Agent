"""
Four different failures, four different consequences.

The live Experian symptom this covers: a response that hit the output-token
budget was reported as PROVIDER_UNAVAILABLE — an outage — because
run_extractor caught the AIError base class. That did three harmful things at
once. It told the consumer to "retry once the service is available", which
would have re-bought an identical failure at full price. It recorded the
failure as costing nothing, when it was the most expensive kind. And it hid
from operators that the problem was our request being too big, not the
provider being down.

What is asserted here is that the four kinds stay apart end to end: in the
stored status, in the retry decision, in the usage row, and in the wording —
while none of the provider's own detail reaches a consumer-facing response.
"""
import pytest

from app.services.ai import AIConfigurationError, AIProviderError, AIRefusalError, AIResponseError
from app.services.ai import ProviderUsage
from app.services.document_extraction.status import (
    OPERATIONAL_MESSAGES, OPERATIONAL_REASONS, ExtractionStatus,
)
from tests.conftest import drain_extraction_queue, requires_db
from tests.test_document_ingestion_flow import (
    SOURCE_PDF_TEXT, FakeDocumentProvider, _pdf, client, document_ai, golden_extraction,
    verifying_audit,
)

pytestmark = requires_db

PDF_BYTES = _pdf(SOURCE_PDF_TEXT)

# What a real Sol truncation looks like: the model spent the whole budget,
# a third of it on reasoning, and returned nothing usable. We were billed.
TRUNCATION = AIResponseError(
    "Document response incomplete: max_output_tokens; spent 32000 output token(s) "
    "(11840 reasoning) of max_output_tokens=32000",
    usage=ProviderUsage(input_tokens=94_317, output_tokens=32_000, reasoning_tokens=11_840,
                        latency_ms=214_000.0, response_id="resp_abc123", max_tokens=32_000),
)
QUOTA = AIProviderError(
    "OpenAI API error 429: You exceeded your current quota (insufficient_quota)"
)


def _raiser(error, *, fail_pass="extract"):
    class Raising(FakeDocumentProvider):
        def __init__(self, *args):
            super().__init__(*args)
            self.passes: list[str] = []

        async def generate_document(self, config, *, output_type, **kwargs):
            from app.services.document_extraction.schema import CreditReportExtraction
            which = "extract" if output_type is CreditReportExtraction else "audit"
            self.passes.append(which)
            if which == fail_pass:
                raise error
            return await super().generate_document(config, output_type=output_type, **kwargs)

    from app.services.ai import register_provider

    provider = Raising(golden_extraction(), verifying_audit())
    register_provider("openai", provider)
    return provider


async def _upload(client):
    response = await client.post(
        "/api/reports/upload", files={"file": ("r.pdf", PDF_BYTES, "application/pdf")},
        data={"bureau": "auto_detect"},
    )
    assert response.status_code == 202, response.text
    report_id = response.json()["report_id"]
    await drain_extraction_queue()
    return report_id


async def _row(report_id):
    from app.database import async_session_maker
    from app.models.credit_report import CreditReport

    async with async_session_maker() as db:
        return await db.get(CreditReport, report_id)


# ── The mapping itself ──────────────────────────────────────────────────

@pytest.mark.parametrize("error_class,expected", [
    ("AIProviderError", ExtractionStatus.PROVIDER_UNAVAILABLE),
    ("AIResponseError", ExtractionStatus.MODEL_RESPONSE_FAILED),
    ("AIRefusalError", ExtractionStatus.MODEL_REFUSED),
    ("AIConfigurationError", ExtractionStatus.CONFIGURATION_ERROR),
    ("DocumentUnsupported", ExtractionStatus.CONFIGURATION_ERROR),
])
def test_every_ai_error_maps_to_its_own_state(error_class, expected):
    # From the live exception...
    assert ExtractionStatus.for_error(f"{error_class}: some detail") is expected
    # ...and from the string an operator record already holds, identically.
    assert ExtractionStatus.for_error(error_class) is expected


def test_an_unknown_failure_is_treated_as_an_outage_not_a_document_fault():
    """Erring toward "our problem, transient" is the safe default: it never
    blames the consumer's PDF for something we don't understand."""
    status = ExtractionStatus.for_error("SomethingNew: ?")
    assert status is ExtractionStatus.PROVIDER_UNAVAILABLE
    assert status.is_operational


def test_only_free_failures_are_retryable():
    """The property that stops a retry button from spending money."""
    assert ExtractionStatus.PROVIDER_UNAVAILABLE.is_retryable
    assert ExtractionStatus.FAILED.is_retryable          # the PDF itself; costs nothing to re-read
    assert not ExtractionStatus.MODEL_RESPONSE_FAILED.is_retryable
    assert not ExtractionStatus.MODEL_REFUSED.is_retryable
    assert not ExtractionStatus.CONFIGURATION_ERROR.is_retryable
    # A billed failure must never also be advertised as retryable.
    for status in ExtractionStatus:
        assert not (status.was_billed and status.is_retryable), status


def test_no_operational_failure_blames_the_consumers_document():
    for status, message in OPERATIONAL_MESSAGES.items():
        assert status.is_operational
        assert "stored safely" in message
        for blame in ("your document couldn't", "unreadable", "scanned", "re-upload", "upload again"):
            assert blame.lower() not in message.lower(), (status, blame)
    # Only a genuinely billed failure is allowed to withhold the retry offer,
    # and only a free one is allowed to make it.
    assert "Retry extraction" in OPERATIONAL_MESSAGES[ExtractionStatus.PROVIDER_UNAVAILABLE]
    assert "retrying won't help" in OPERATIONAL_MESSAGES[ExtractionStatus.MODEL_RESPONSE_FAILED]


def test_the_outage_reason_is_not_reused_for_a_billed_failure():
    """"AI extraction was unavailable" is simply untrue when the model ran,
    answered, and billed us."""
    outage = OPERATIONAL_REASONS[ExtractionStatus.PROVIDER_UNAVAILABLE]
    billed = OPERATIONAL_REASONS[ExtractionStatus.MODEL_RESPONSE_FAILED]
    assert "unavailable" in outage
    assert "unavailable" not in billed
    assert outage != billed


# ── End to end through the worker ───────────────────────────────────────

async def test_a_token_budget_truncation_is_not_reported_as_an_outage(client, document_ai):
    """The exact live symptom. It must land as MODEL_RESPONSE_FAILED."""
    _raiser(TRUNCATION)
    report_id = await _upload(client)

    row = await _row(report_id)
    assert row.extraction_status == "model_response_failed"
    assert row.processing_stage == "model_response_failed"
    assert row.last_processing_error_class == "AIResponseError"

    # The operator record keeps the real reason, the budget and the response id.
    failure = (row.extraction_audit or {})["failure"]
    assert failure["status"] == "model_response_failed"
    assert failure["pass"] == "extract"
    assert failure["billed"] is True
    assert (failure["input_tokens"], failure["output_tokens"]) == (94_317, 32_000)
    assert failure["reasoning_tokens"] == 11_840
    assert failure["max_output_tokens"] == 32_000
    assert failure["response_id"] == "resp_abc123"


async def test_a_billed_failure_is_not_offered_as_a_retry(client, document_ai):
    """Offering "retry" here would spend the same money for the same result."""
    _raiser(TRUNCATION)
    report_id = await _upload(client)

    detail = (await client.get(f"/api/reports/{report_id}")).json()
    assert detail["extraction_retryable"] is False

    refused = await client.post(f"/api/reports/{report_id}/retry-extraction")
    assert refused.status_code == 409
    message = refused.json()["detail"]
    assert "won't help" in message
    assert "nothing is wrong with your document" in message.lower()


async def test_a_real_outage_is_still_retryable(client, document_ai):
    """The distinction cuts both ways: a free failure keeps its retry."""
    provider = _raiser(QUOTA)
    report_id = await _upload(client)

    row = await _row(report_id)
    assert row.extraction_status == "provider_unavailable"
    assert row.last_processing_error_class == "AIProviderError"
    assert (row.extraction_audit or {})["failure"]["billed"] is False

    detail = (await client.get(f"/api/reports/{report_id}")).json()
    assert detail["extraction_retryable"] is True
    assert (await client.post(f"/api/reports/{report_id}/retry-extraction")).status_code == 202

    # And the retry actually works once the outage clears.
    from app.services.ai import register_provider

    register_provider("openai", FakeDocumentProvider(golden_extraction(), verifying_audit()))
    await drain_extraction_queue()
    assert (await _row(report_id)).extraction_status == "verified"
    assert provider.passes == ["extract"]


@pytest.mark.parametrize("error,expected", [
    (AIRefusalError("Model declined the document request: policy"), "model_refused"),
    (AIConfigurationError("OpenAI authentication failed"), "configuration_error"),
])
async def test_refusal_and_misconfiguration_get_their_own_states(
    client, document_ai, error, expected
):
    _raiser(error)
    report_id = await _upload(client)
    row = await _row(report_id)
    assert row.extraction_status == expected
    assert ExtractionStatus(row.extraction_status).is_retryable is False


async def test_a_failed_audit_keeps_its_own_failure_class(client, document_ai):
    """A truncated AUDIT must not be filed as an outage either — and must
    still not cost a second extraction."""
    provider = _raiser(TRUNCATION, fail_pass="audit")
    report_id = await _upload(client)

    row = await _row(report_id)
    # Pass 1 succeeded, so the report is held at needs_audit, not failed.
    assert row.extraction_status == "needs_audit"
    assert row.extraction_checkpoint["extraction"]["accounts"]
    failure = row.extraction_checkpoint["audit_failure"]
    assert failure["error_class"] == "AIResponseError"
    assert failure["status"] == "model_response_failed"
    assert failure["billed"] is True
    assert provider.passes == ["extract", "audit"]


# ── Cost accounting ─────────────────────────────────────────────────────

async def test_a_billed_failure_records_what_it_cost(client, document_ai):
    """The gap that made the live failure's spend unanswerable: a failed call
    recorded zero tokens and no cost, because the usage was only read on the
    success path."""
    from app.database import async_session_maker
    from app.models.ai_usage import AIUsageLog
    from app.services.ai import add_usage_listener, clear_usage_listeners
    from app.services.usage_sink import persist_usage
    from sqlalchemy import select

    clear_usage_listeners()
    add_usage_listener(persist_usage)
    try:
        _raiser(TRUNCATION)
        report_id = await _upload(client)
    finally:
        clear_usage_listeners()

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(AIUsageLog).where(AIUsageLog.context["report_id"].astext == str(report_id))
        )).scalars().all()

    assert len(rows) == 1
    row = rows[0]
    assert row.success is False
    assert (row.input_tokens, row.output_tokens) == (94_317, 32_000)
    assert row.latency_ms == pytest.approx(214_000.0)
    # gpt-5.6-sol at $4/$20 per 1M: 94,317×4 + 32,000×20 = $1.0173
    assert row.estimated_cost_usd == pytest.approx(1.017268, abs=1e-5)
    assert row.context["response_id"] == "resp_abc123"
    assert row.context["max_output_tokens"] == 32_000
    assert row.context["reasoning_tokens"] == 11_840
    assert "AIResponseError" in row.error


async def test_a_free_failure_still_records_no_spend(client, document_ai):
    """A call that never ran must not be made to look expensive either."""
    from app.database import async_session_maker
    from app.models.ai_usage import AIUsageLog
    from app.services.ai import add_usage_listener, clear_usage_listeners
    from app.services.usage_sink import persist_usage
    from sqlalchemy import select

    clear_usage_listeners()
    add_usage_listener(persist_usage)
    try:
        _raiser(QUOTA)
        report_id = await _upload(client)
    finally:
        clear_usage_listeners()

    async with async_session_maker() as db:
        row = (await db.execute(
            select(AIUsageLog).where(AIUsageLog.context["report_id"].astext == str(report_id))
        )).scalar_one()
    assert (row.input_tokens, row.output_tokens) == (0, 0)
    assert row.estimated_cost_usd in (None, 0.0)


# ── Nothing leaks ───────────────────────────────────────────────────────

@pytest.mark.parametrize("error", [TRUNCATION, QUOTA,
                                   AIRefusalError("declined: policy"),
                                   AIConfigurationError("OpenAI authentication failed: bad key")])
async def test_no_failure_class_leaks_provider_internals(client, document_ai, error):
    _raiser(error)
    report_id = await _upload(client)

    for path in (f"/api/reports/{report_id}/status", f"/api/reports/{report_id}", "/api/reports/"):
        body = (await client.get(path)).text.lower()
        for leak in ("openai", "429", "quota", "max_output_tokens", "resp_abc123",
                     "airesponseerror", "aiproviderror", "reasoning", "94317", "32000",
                     "authentication"):
            assert leak not in body, f"{leak} leaked into {path}"
