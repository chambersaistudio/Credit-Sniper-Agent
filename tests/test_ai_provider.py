import pytest

from app.services.ai.provider import (
    ModelTier,
    resolve_tier,
    complete,
    _provider_instances,
    _get_provider_instance,
)


@pytest.fixture(autouse=True)
def _reset_provider_cache():
    _provider_instances.clear()
    yield
    _provider_instances.clear()


def test_resolve_tier_uses_defaults_when_no_override(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ai_fast_provider", "")
    monkeypatch.setattr(settings, "ai_fast_model", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")

    provider, model = resolve_tier(ModelTier.FAST)
    assert provider.name == "anthropic"
    assert model == "claude-haiku-4-5-20251001"


def test_resolve_tier_respects_env_override(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ai_reasoning_provider", "openai")
    monkeypatch.setattr(settings, "ai_reasoning_model", "gpt-5")
    monkeypatch.setattr(settings, "openai_api_key", "test-key")

    provider, model = resolve_tier(ModelTier.REASONING)
    assert provider.name == "openai"
    assert model == "gpt-5"


def test_unknown_provider_raises():
    from app.config import settings

    with pytest.raises(ValueError):
        _get_provider_instance("not-a-real-provider")


def test_complete_returns_result_and_logs(monkeypatch):
    from app.services.ai import provider as provider_module

    class FakeProvider:
        name = "fake"

        def complete(self, *, system, prompt, model, max_tokens):
            return provider_module.CompletionResult(
                text="hello",
                provider="fake",
                model=model,
                input_tokens=10,
                output_tokens=5,
                latency_ms=1.0,
            )

    monkeypatch.setitem(provider_module._PROVIDER_FACTORIES, "anthropic", lambda: FakeProvider())
    monkeypatch.setattr(provider_module.settings, "ai_fast_provider", "")
    monkeypatch.setattr(provider_module.settings, "ai_fast_model", "")

    result = complete(ModelTier.FAST, system="sys", prompt="hi", max_tokens=100, task="unit_test")
    assert result.text == "hello"
    assert result.provider == "fake"
