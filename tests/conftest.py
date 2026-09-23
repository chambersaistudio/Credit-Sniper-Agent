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


requires_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="set TEST_DATABASE_URL to run integration tests")


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
