"""
Provider adapters. The only modules in the app allowed to import a vendor
SDK. Each adapter takes a resolved TierConfig plus a Pydantic output type
and returns a validated instance with token usage — or raises one of the
AI layer's own error types.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from app.config import settings
from app.services.ai.config import TierConfig
from app.services.ai.errors import (
    AIConfigurationError,
    AIProviderError,
    AIRefusalError,
    AIResponseError,
    ProviderUsage,
)

T = TypeVar("T", bound=BaseModel)


@dataclass
class ProviderResult(Generic[T]):
    output: T
    provider: str
    model: str  # the model that actually served the request (may differ after a fallback)
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    latency_ms: float


class Provider(Protocol):
    name: str

    async def generate(
        self, config: TierConfig, *, system: str, prompt: str, output_type: type[T], max_tokens: int
    ) -> ProviderResult[T]: ...


class DocumentProvider(Protocol):
    """A provider that reads an ORIGINAL document (PDF bytes) rather than
    text we extracted for it. Implemented per vendor; the data model and the
    calling code stay vendor-neutral."""

    name: str

    async def generate_document(
        self, config: TierConfig, *, system: str, prompt: str, document: bytes, filename: str,
        output_type: type[T], max_tokens: int, detail: str = "high",
    ) -> ProviderResult[T]: ...


class DocumentUnsupported(AIConfigurationError):
    """Raised by providers that can't accept a raw document."""


def _text_format_param(output_type: type[BaseModel]) -> dict:
    """The Responses `text.format` payload for a strict structured output.

    Prefers the SDK's own converter so the schema matches exactly what
    responses.parse would have sent, and falls back to building it directly if
    that private helper moves — the only thing it does is name the schema and
    mark it strict."""
    try:
        from openai.lib._parsing._responses import type_to_text_format_param

        return dict(type_to_text_format_param(output_type))
    except Exception:  # pragma: no cover - SDK layout change
        return {
            "type": "json_schema",
            "strict": True,
            "name": output_type.__name__,
            "schema": output_type.model_json_schema(),
        }


class AnthropicProvider:
    name = "anthropic"

    def __init__(self) -> None:
        import anthropic

        self._anthropic = anthropic
        # No key argument when unset: the SDK then resolves credentials from
        # ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / an `ant auth` profile.
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None)

    async def generate(self, config, *, system, prompt, output_type, max_tokens):
        from anthropic.lib._parse._transform import transform_schema

        output_config: dict = {
            "format": {"type": "json_schema", "schema": transform_schema(output_type.model_json_schema())}
        }
        if config.effort:
            output_config["effort"] = config.effort

        kwargs: dict = {
            "model": config.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": output_config,
        }
        if config.refusal_fallback:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"

        start = time.monotonic()
        try:
            # Streamed so long adaptive-thinking turns at high effort don't
            # hit non-streaming HTTP timeouts.
            async with self._client.beta.messages.stream(**kwargs) as stream:
                message = await stream.get_final_message()
        except self._anthropic.AuthenticationError as e:
            raise AIConfigurationError(f"Anthropic authentication failed: {e.message}") from e
        except self._anthropic.APIStatusError as e:
            raise AIProviderError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except self._anthropic.APIConnectionError as e:
            raise AIProviderError(f"Could not reach Anthropic API: {e}") from e
        except self._anthropic.AnthropicError as e:
            raise AIConfigurationError(f"Anthropic client error: {e}") from e
        latency_ms = (time.monotonic() - start) * 1000

        if message.stop_reason == "refusal":
            category = getattr(message.stop_details, "category", None) if message.stop_details else None
            raise AIRefusalError(f"Model declined the request (category={category})")
        if message.stop_reason == "max_tokens":
            raise AIResponseError(f"Response truncated at max_tokens={max_tokens}")

        text = "".join(block.text for block in message.content if block.type == "text")
        try:
            output = output_type.model_validate_json(text)
        except ValidationError as e:
            raise AIResponseError(f"Model output failed schema validation: {e}") from e

        usage = message.usage
        return ProviderResult(
            output=output,
            provider=self.name,
            model=message.model,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_read_tokens=usage.cache_read_input_tokens or 0,
            cache_write_tokens=usage.cache_creation_input_tokens or 0,
            latency_ms=latency_ms,
        )


class OpenAIProvider:
    name = "openai"

    def __init__(self) -> None:
        import openai

        self._openai = openai
        self._client = openai.AsyncOpenAI(api_key=settings.openai_api_key or None)

    async def generate(self, config, *, system, prompt, output_type, max_tokens):
        start = time.monotonic()
        try:
            completion = await self._client.chat.completions.parse(
                model=config.model,
                max_completion_tokens=max_tokens,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                response_format=output_type,
            )
        except self._openai.LengthFinishReasonError as e:
            raise AIResponseError(f"Response truncated at max_tokens={max_tokens}") from e
        except self._openai.AuthenticationError as e:
            raise AIConfigurationError(f"OpenAI authentication failed: {e.message}") from e
        except self._openai.APIStatusError as e:
            raise AIProviderError(f"OpenAI API error {e.status_code}: {e.message}") from e
        except self._openai.APIConnectionError as e:
            raise AIProviderError(f"Could not reach OpenAI API: {e}") from e
        except self._openai.OpenAIError as e:
            raise AIConfigurationError(f"OpenAI client error: {e}") from e
        except ValidationError as e:
            raise AIResponseError(f"Model output failed schema validation: {e}") from e
        latency_ms = (time.monotonic() - start) * 1000

        message = completion.choices[0].message
        if message.refusal:
            raise AIRefusalError(f"Model declined the request: {message.refusal}")
        if message.parsed is None:
            raise AIResponseError("Model returned no structured output")

        usage = completion.usage
        cached = 0
        if usage and usage.prompt_tokens_details and usage.prompt_tokens_details.cached_tokens:
            cached = usage.prompt_tokens_details.cached_tokens
        return ProviderResult(
            output=message.parsed,
            provider=self.name,
            model=completion.model,
            input_tokens=(usage.prompt_tokens - cached) if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            cache_read_tokens=cached,
            cache_write_tokens=0,
            latency_ms=latency_ms,
        )

    def build_document_input(self, prompt: str, document: bytes, filename: str, detail: str) -> list[dict]:
        """The Responses API input for one document + instruction.

        The PDF is sent inline as base64 `file_data` rather than uploaded as a
        persistent Files object, so nothing about the report outlives the
        request. Kept separate from the call so the exact payload shape is
        unit-testable without a network round trip."""
        import base64

        encoded = base64.b64encode(document).decode("ascii")
        return [{
            "role": "user",
            "content": [
                {
                    "type": "input_file",
                    "filename": filename,
                    "file_data": f"data:application/pdf;base64,{encoded}",
                    "detail": detail,
                },
                {"type": "input_text", "text": prompt},
            ],
        }]

    @staticmethod
    def _response_usage(response, latency_ms: float, max_tokens: int) -> ProviderUsage:
        """What this response cost us, whether or not it was usable.

        A response that stopped at the token budget is billed in full, so this
        is read on the failure paths too — otherwise the most expensive
        failures are the ones that record zero spend."""
        usage = getattr(response, "usage", None)
        cached = 0
        reasoning = None
        if usage:
            details = getattr(usage, "input_tokens_details", None)
            cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
            out_details = getattr(usage, "output_tokens_details", None)
            if out_details is not None:
                reasoning = getattr(out_details, "reasoning_tokens", None)
        return ProviderUsage(
            input_tokens=((getattr(usage, "input_tokens", 0) or 0) - cached) if usage else 0,
            output_tokens=(getattr(usage, "output_tokens", 0) or 0) if usage else 0,
            cache_read_tokens=cached,
            cache_write_tokens=0,
            latency_ms=latency_ms,
            response_id=getattr(response, "id", None),
            max_tokens=max_tokens,
            reasoning_tokens=reasoning,
        )

    async def generate_document(
        self, config, *, system, prompt, document, filename, output_type, max_tokens, detail="high"
    ):
        start = time.monotonic()
        try:
            # responses.create plus a local parse — deliberately NOT
            # responses.parse. The parse helper validates inside the SDK and
            # raises before the Response is ever returned, so a truncated
            # answer (the most expensive failure there is: billed in full,
            # worth nothing) arrives with no usage attached. Parsing here
            # keeps the response, and therefore the bill, in hand.
            response = await self._client.responses.create(
                model=config.model,
                instructions=system,
                input=self.build_document_input(prompt, document, filename, detail),
                text={"format": _text_format_param(output_type)},
                max_output_tokens=max_tokens,
                # No Responses application-state persistence, and no
                # Conversation object carrying the consumer's report. This is
                # not the same as zero retention: OpenAI's standard abuse
                # monitoring may still retain request data for up to 30 days
                # unless the project is approved for Zero Data Retention.
                store=False,
            )
        except self._openai.LengthFinishReasonError as e:
            raise AIResponseError(
                f"Document response truncated at max_output_tokens={max_tokens}",
                usage=ProviderUsage(latency_ms=(time.monotonic() - start) * 1000, max_tokens=max_tokens),
            ) from e
        except self._openai.AuthenticationError as e:
            raise AIConfigurationError(f"OpenAI authentication failed: {e.message}") from e
        except self._openai.APIStatusError as e:
            # Deliberately only the status and the provider's own message —
            # never the request body, which carries the report.
            raise AIProviderError(f"OpenAI API error {e.status_code}: {e.message}") from e
        except self._openai.APIConnectionError as e:
            raise AIProviderError(f"Could not reach OpenAI API: {e}") from e
        except self._openai.OpenAIError as e:
            raise AIConfigurationError(f"OpenAI client error: {e}") from e
        except ValidationError as e:
            raise AIResponseError(f"Model output failed schema validation: {e}") from e
        latency_ms = (time.monotonic() - start) * 1000

        # Everything below this point was BILLED. Each failure carries the
        # usage it consumed so the cost of failing is recorded, not lost.
        billed = self._response_usage(response, latency_ms, max_tokens)

        if getattr(response, "status", None) == "incomplete":
            reason = getattr(getattr(response, "incomplete_details", None), "reason", "unknown")
            # On the Responses API max_output_tokens caps reasoning AND the
            # answer together, so a reasoning model can spend the whole budget
            # thinking and return nothing usable. Say what it spent.
            spent = (f"; spent {billed.output_tokens} output token(s)"
                     f"{f' ({billed.reasoning_tokens} reasoning)' if billed.reasoning_tokens else ''}"
                     f" of max_output_tokens={max_tokens}")
            raise AIResponseError(f"Document response incomplete: {reason}{spent}", usage=billed)

        refusal = next(
            (c.refusal for item in (response.output or []) for c in (getattr(item, "content", None) or [])
             if getattr(c, "type", None) == "refusal"),
            None,
        )
        if refusal:
            raise AIRefusalError(f"Model declined the document request: {refusal}", usage=billed)

        text = response.output_text
        if not text:
            raise AIResponseError("Model returned no structured output for the document", usage=billed)
        try:
            parsed = output_type.model_validate_json(text)
        except ValidationError as e:
            # Where a truncated answer actually lands. The model stopped
            # mid-string, so the JSON never closed. How many characters it did
            # produce is the measurement that says how far over budget the
            # request is, so it goes in the message.
            raise AIResponseError(
                f"Model output failed schema validation after {len(text):,} chars "
                f"(max_output_tokens={max_tokens}): {e}",
                usage=billed,
            ) from e

        return ProviderResult(
            output=parsed,
            provider=self.name,
            model=response.model,
            input_tokens=billed.input_tokens,
            output_tokens=billed.output_tokens,
            cache_read_tokens=billed.cache_read_tokens,
            cache_write_tokens=billed.cache_write_tokens,
            latency_ms=latency_ms,
        )


_FACTORIES = {"anthropic": AnthropicProvider, "openai": OpenAIProvider}
_instances: dict[str, Provider] = {}


def get_provider(name: str) -> Provider:
    if name not in _FACTORIES:
        raise AIConfigurationError(f"Unknown AI provider: {name!r}")
    if name not in _instances:
        try:
            _instances[name] = _FACTORIES[name]()
        except ImportError as e:
            raise AIConfigurationError(f"SDK for provider {name!r} is not installed") from e
    return _instances[name]


def register_provider(name: str, provider: Provider) -> None:
    """Install a provider instance directly (tests, or a new vendor adapter)."""
    _instances[name] = provider


def get_document_provider(name: str) -> DocumentProvider:
    provider = get_provider(name)
    if not hasattr(provider, "generate_document"):
        raise DocumentUnsupported(f"Provider {name!r} cannot read documents directly")
    return provider  # type: ignore[return-value]
