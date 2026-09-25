import os
import sys
import tempfile
from typing import Any, Callable

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

# Integration tests run against a real Postgres when TEST_DATABASE_URL is
# set; they're skipped otherwise. Must be configured before app import.
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
if TEST_DATABASE_URL:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("DB_NULL_POOL", "1")
# Extraction is a durable background job in production. Tests drive it
# explicitly (drain_extraction_queue) so each pass is observable and no loop
# races the assertions.
os.environ.setdefault("EXTRACTION_WORKER_ENABLED", "0")
os.environ.setdefault("UPLOAD_DIR", tempfile.mkdtemp(prefix="credit-sniper-test-"))

from app.services.ai import clear_usage_listeners, register_provider  # noqa: E402
from app.services.ai.config import TierConfig  # noqa: E402
from app.services.ai.providers import ProviderResult, _instances  # noqa: E402


class FakeProvider:
    """Stands in for a vendor SDK. `responder(output_type, prompt, config)`
    returns the Pydantic instance (or raises an AIError)."""

    name = "anthropic"

    def __init__(self, responder: Callable[[type, str, TierConfig], Any]):
        self.responder = responder
        self.calls: list[dict[str, Any]] = []

    async def generate(self, config, *, system, prompt, output_type, max_tokens):
        self.calls.append({"config": config, "prompt": prompt, "output_type": output_type})
        output = self.responder(output_type, prompt, config)
        return ProviderResult(
            output=output, provider=self.name, model=config.model, input_tokens=100, output_tokens=50,
            cache_read_tokens=0, cache_write_tokens=0, latency_ms=1.0,
        )


@pytest.fixture
def fake_ai():
    """Install a FakeProvider for the default provider; returns a setter."""
    saved = dict(_instances)
    clear_usage_listeners()
    holder: dict[str, FakeProvider] = {}

    def install(responder):
        holder["provider"] = FakeProvider(responder)
        register_provider("anthropic", holder["provider"])
        return holder["provider"]

    yield install
    _instances.clear()
    _instances.update(saved)
    clear_usage_listeners()


async def drain_extraction_queue(limit: int = 50) -> list[str]:
    """Run the background extraction worker until the queue is empty.

    Uploading returns 202 and enqueues; nothing is extracted inside the
    request. Tests call this where production would have the worker loop
    pick the job up, then read the finished report."""
    from app.services.extraction_jobs import claim_next, process_report

    stages = []
    for _ in range(limit):
        report_id = await claim_next()
        if report_id is None:
            break
        stages.append(await process_report(report_id))
    return stages


async def upload_and_process(client, pdf_bytes, *, bureau="auto_detect", headers=None):
    """Upload a report, run the background job, and return the finished
    report as the detail endpoint reports it."""
    response = await client.post(
        "/api/reports/upload",
        files={"file": ("r.pdf", pdf_bytes, "application/pdf")},
        data={"bureau": bureau},
        **({"headers": headers} if headers else {}),
    )
    assert response.status_code == 202, response.text
    report_id = response.json()["report_id"]
    await drain_extraction_queue()
    return await client.get(f"/api/reports/{report_id}", **({"headers": headers} if headers else {}))


# Anything that would identify the AI vendor, its status codes or its token
# accounting to a consumer. Checked against responses that must stay generic.
PROVIDER_INTERNALS = (
    "openai", "anthropic", "gpt-", "claude-", "quota", "insufficient_quota",
    "credit_balance_exhausted", "airesponseerror", "aiprovidererror",
    "airefusalerror", "aiconfigurationerror", "max_output_tokens",
    "reasoning_tokens", "response_id", "last_processing_error_class",
    "api error", "status code", "resp_",
)


def assert_no_provider_internals(text: str, *, extra: tuple[str, ...] = ()) -> None:
    """Fail if a consumer-facing payload names the provider or its internals.

    UUIDs and timestamps are stripped first: a report id like
    a794429a-... contains "429", and a bare-substring check against raw digits
    would report a leak that is not there."""
    import re

    scrubbed = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>",
                      text, flags=re.I)
    scrubbed = re.sub(r"\d{4}-\d{2}-\d{2}t[\d:.+-]+", "<ts>", scrubbed, flags=re.I).lower()
    for leak in PROVIDER_INTERNALS + extra:
        assert leak not in scrubbed, f"{leak!r} leaked into a consumer-facing response"


requires_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="set TEST_DATABASE_URL to run integration tests")


async def mark_reports_verified() -> None:
    """Promote every stored report to VERIFIED.

    Parser-path ingestion is capped at NEEDS_AUDIT by design: only a
    document-model reading of the original PDF that survives its independent
    audit can be VERIFIED, and dispute evaluation requires VERIFIED. Lifecycle
    tests here upload through the parser path, so they promote their reports
    to stand in for a real AI-native ingestion. That ingestion is covered end
    to end in tests/test_document_ingestion_flow.py.
    """
    from sqlalchemy import update

    from app.database import async_session_maker
    from app.models.credit_report import CreditReport

    async with async_session_maker() as session:
        await session.execute(update(CreditReport).values(extraction_status="verified"))
        await session.commit()


@pytest.fixture
async def db_ready():
    """Fresh schema for each integration test, built by the real migrations."""
    from sqlalchemy import text

    from app.database import engine, run_migrations

    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await run_migrations()
    yield
