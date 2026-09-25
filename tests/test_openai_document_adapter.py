"""
The OpenAI document adapter, against a stubbed client.

The production failure this pins down: Sol produced ~111,000 characters of
JSON and stopped mid-string, so pydantic raised

    Invalid JSON: EOF while parsing a string at line 1 column 112636

We had been calling `responses.parse`, which validates inside the SDK and
raises before returning the Response — so the usage block came back with the
exception and was thrown away. Both attempts recorded zero tokens and no cost
for work that cost about a dollar each.

The adapter now calls `responses.create` and parses locally, which keeps the
Response — and therefore the bill — in hand on every failure path.
"""
import pytest
from pydantic import BaseModel

import openai

from app.services.ai import AIRefusalError, AIResponseError
from app.services.ai.config import TierConfig
from app.services.ai.providers import OpenAIProvider


class Tiny(BaseModel):
    name: str
    pages: list[int]


VALID = '{"name": "ATLAS", "pages": [3]}'
# Truncated exactly the way the live failure was: cut off inside a string.
TRUNCATED = '{"name": "ATLAS", "pages": [3], "extra": "unterminated strin'


class _Usage:
    def __init__(self, input_tokens, output_tokens, cached=0, reasoning=None):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.input_tokens_details = type("D", (), {"cached_tokens": cached})()
        self.output_tokens_details = type("D", (), {"reasoning_tokens": reasoning})()


class _Content:
    def __init__(self, type_, text=None, refusal=None):
        self.type = type_
        self.text = text
        self.refusal = refusal


class _Item:
    def __init__(self, content):
        self.type = "message"
        self.content = content


class _Response:
    def __init__(self, text=None, *, status="completed", refusal=None, usage=None,
                 incomplete_reason=None):
        self.id = "resp_live_001"
        self.model = "gpt-5.6-sol"
        self.status = status
        self.usage = usage or _Usage(94_317, 32_000, reasoning=11_840)
        self.incomplete_details = (
            type("I", (), {"reason": incomplete_reason})() if incomplete_reason else None
        )
        content = []
        if refusal is not None:
            content.append(_Content("refusal", refusal=refusal))
        if text is not None:
            content.append(_Content("output_text", text=text))
        self.output = [_Item(content)] if content else []

    @property
    def output_text(self):
        return "".join(c.text for item in self.output for c in item.content
                       if c.type == "output_text" and c.text)


def _provider(response=None, raises=None):
    """An OpenAIProvider wired to a stub client, with the real SDK's exception
    classes so the adapter's except clauses are exercised as written."""
    provider = object.__new__(OpenAIProvider)
    provider._openai = openai
    calls = []

    class _Responses:
        async def create(self, **kwargs):
            calls.append(kwargs)
            if raises:
                raise raises
            return response

    provider._client = type("C", (), {"responses": _Responses()})()
    provider.calls = calls
    return provider


async def _run(provider, output_type=Tiny, max_tokens=32_000):
    return await provider.generate_document(
        TierConfig(provider="openai", model="gpt-5.6-sol", max_tokens=max_tokens),
        system="sys", prompt="do it", document=b"%PDF-1.4 x", filename="r.pdf",
        output_type=output_type, max_tokens=max_tokens, detail="high",
    )


# ── The request ─────────────────────────────────────────────────────────

async def test_the_pdf_is_sent_inline_and_not_persisted():
    provider = _provider(_Response(VALID))
    await _run(provider)
    kwargs = provider.calls[0]

    assert kwargs["store"] is False, "the consumer's report must not persist as provider state"
    assert kwargs["max_output_tokens"] == 32_000
    assert kwargs["text"]["format"]["type"] == "json_schema"
    assert kwargs["text"]["format"]["strict"] is True
    content = kwargs["input"][0]["content"]
    file_part = next(c for c in content if c["type"] == "input_file")
    assert file_part["file_data"].startswith("data:application/pdf;base64,")
    assert file_part["detail"] == "high"


async def test_a_good_response_parses_and_reports_its_usage():
    provider = _provider(_Response(VALID, usage=_Usage(41_200, 1_158, cached=200)))
    result = await _run(provider)

    assert result.output == Tiny(name="ATLAS", pages=[3])
    assert result.input_tokens == 41_000        # cached tokens counted separately
    assert result.cache_read_tokens == 200
    assert result.output_tokens == 1_158
    assert result.model == "gpt-5.6-sol"


# ── The live failure ────────────────────────────────────────────────────

async def test_truncated_json_is_a_response_failure_that_knows_what_it_cost():
    """The exact production failure. It must arrive classified AND priced."""
    provider = _provider(_Response(TRUNCATED))

    with pytest.raises(AIResponseError) as caught:
        await _run(provider)

    error = caught.value
    # The measurement that says how far over budget the request is.
    assert "chars" in str(error)
    assert "max_output_tokens=32000" in str(error)
    # Structural, not pydantic's own string: that one quotes the offending
    # INPUT, which on a credit report is the model's rendering of somebody's
    # accounts, and it would land in a stored failure record.
    assert "schema error(s)" in str(error)
    assert "json_invalid" in str(error)
    assert "unterminated strin" not in str(error), "model output leaked into the failure"

    # And — the part that was missing — what it was billed.
    assert error.usage is not None
    assert error.usage.input_tokens == 94_317
    assert error.usage.output_tokens == 32_000
    assert error.usage.reasoning_tokens == 11_840
    assert error.usage.max_tokens == 32_000
    assert error.usage.response_id == "resp_live_001"


async def test_truncated_json_maps_to_model_response_failed_not_an_outage():
    from app.services.document_extraction.status import ExtractionStatus

    provider = _provider(_Response(TRUNCATED))
    with pytest.raises(AIResponseError) as caught:
        await _run(provider)
    status = ExtractionStatus.for_error(caught.value)
    assert status is ExtractionStatus.MODEL_RESPONSE_FAILED
    assert not status.is_retryable, "retrying would re-buy an identical failure"


async def test_an_incomplete_response_also_carries_its_usage():
    provider = _provider(_Response(None, status="incomplete",
                                   incomplete_reason="max_output_tokens"))
    with pytest.raises(AIResponseError) as caught:
        await _run(provider)
    assert "max_output_tokens" in str(caught.value)
    assert caught.value.usage.output_tokens == 32_000
    assert caught.value.usage.reasoning_tokens == 11_840


async def test_a_refusal_is_a_refusal_and_is_also_billed():
    provider = _provider(_Response(None, refusal="I can't help with that"))
    with pytest.raises(AIRefusalError) as caught:
        await _run(provider)
    assert "I can't help with that" in str(caught.value)
    assert caught.value.usage.output_tokens == 32_000


async def test_an_empty_response_is_a_response_failure_not_a_silent_success():
    provider = _provider(_Response(None))
    with pytest.raises(AIResponseError) as caught:
        await _run(provider)
    assert "no structured output" in str(caught.value)
    assert caught.value.usage is not None


# ── Transport failures stay free and stay transient ─────────────────────

def _auth_error():
    import httpx

    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return openai.AuthenticationError(
        "bad key", response=httpx.Response(401, request=request), body=None
    )


def _status_error():
    import httpx

    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return openai.APIStatusError(
        "rate limited", response=httpx.Response(429, request=request), body=None
    )


@pytest.mark.parametrize("make_error,expected", [
    (lambda: openai.APIConnectionError(request=None), "AIProviderError"),
    (_status_error, "AIProviderError"),
    (_auth_error, "AIConfigurationError"),
])
async def test_transport_failures_are_not_reported_as_billed(make_error, expected):
    from app.services.ai import AIConfigurationError, AIProviderError

    provider = _provider(raises=make_error())
    expected_type = {"AIProviderError": AIProviderError,
                     "AIConfigurationError": AIConfigurationError}[expected]
    with pytest.raises(expected_type) as caught:
        await _run(provider)
    # Nothing ran, so nothing may be recorded as spent.
    assert caught.value.usage is None


async def test_a_provider_error_never_echoes_the_request_body():
    """The request body carries the consumer's report."""
    provider = _provider(raises=openai.APIConnectionError(request=None))
    from app.services.ai import AIProviderError

    with pytest.raises(AIProviderError) as caught:
        await _run(provider)
    assert "base64" not in str(caught.value)
    assert "%PDF" not in str(caught.value)
