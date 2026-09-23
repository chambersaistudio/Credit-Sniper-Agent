"""The Anthropic adapter against a mocked HTTP transport: request shape,
streamed structured-output parsing, and stop-reason handling. Catches SDK
upgrades that change wire behavior before a real call does."""
import json

import anthropic
import httpx2
import pytest

from app.services.ai import AIRefusalError, AIResponseError, ModelTier, resolve_tier
from app.services.ai.providers import AnthropicProvider
from app.services.reasoning_engine import ClaimProposalOut

PAYLOAD = json.dumps({
    "has_dispute_ground": False, "reasoning": "ok", "supporting_finding_ids": [], "disputed_fields": [],
    "recipients": [], "legal_basis": [], "requested_remedy": None, "additional_evidence_needed": [],
    "recommended_action": "no_dispute", "confidence": 0.9,
})


def _sse(text: str, stop_reason: str, stop_details=None, model="claude-opus-5") -> str:
    events = [
        {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 1, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None, "stop_details": stop_details},
         "usage": {"output_tokens": 40}},
        {"type": "message_stop"},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


def _provider(body: str, seen: dict) -> AnthropicProvider:
    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta")
        return httpx2.Response(200, text=body, headers={"content-type": "text/event-stream"})

    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider._anthropic = anthropic
    provider._client = anthropic.AsyncAnthropic(api_key="test", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    return provider


async def _generate(body: str, seen: dict | None = None):
    return await _provider(body, seen if seen is not None else {}).generate(
        resolve_tier(ModelTier.REASONING), system="s", prompt="p", output_type=ClaimProposalOut, max_tokens=100,
    )


async def test_request_shape_and_parsing():
    seen: dict = {}
    result = await _generate(_sse(PAYLOAD, "end_turn", model="claude-opus-4-8"), seen)
    body = seen["body"]
    assert body["model"] == "claude-opus-5" and body["stream"] is True
    assert body["output_config"]["effort"] == "high"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["fallbacks"] == "default" and seen["beta"] == "server-side-fallback-2026-07-01"
    assert "temperature" not in body
    assert result.output.recommended_action == "no_dispute"
    assert (result.model, result.input_tokens, result.output_tokens, result.cache_read_tokens) == ("claude-opus-4-8", 12, 40, 3)


async def test_refusal_raises():
    with pytest.raises(AIRefusalError):
        await _generate(_sse("", "refusal", {"type": "refusal", "category": "cyber", "explanation": None}))


async def test_truncation_raises():
    with pytest.raises(AIResponseError, match="truncated"):
        await _generate(_sse(PAYLOAD[:40], "max_tokens"))


async def test_schema_mismatch_raises():
    with pytest.raises(AIResponseError, match="schema"):
        await _generate(_sse('{"has_dispute_ground": "maybe"}', "end_turn"))
