import pytest
from pydantic import BaseModel

from app.services.ai import (
    AIConfigurationError, AIRefusalError, ModelTier, add_usage_listener, generate, resolve_tier,
)
from app.services.ai.config import estimate_cost_usd


class Echo(BaseModel):
    text: str


def test_default_tiers(monkeypatch):
    from app.config import settings
    for tier in ModelTier:
        for knob in ("provider", "model", "effort"):
            monkeypatch.setattr(settings, f"ai_{tier.value}_{knob}", "")
    assert resolve_tier(ModelTier.FAST).model == "claude-haiku-4-5"
    assert resolve_tier(ModelTier.FAST).effort is None  # Haiku 4.5 doesn't take effort
    assert (resolve_tier(ModelTier.REASONING).model, resolve_tier(ModelTier.REASONING).effort) == ("claude-opus-5", "high")
    assert resolve_tier(ModelTier.ESCALATION).effort == "max"


def test_repointing_a_tier_drops_provider_specific_knobs(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "ai_reasoning_provider", "openai")
    monkeypatch.setattr(settings, "ai_reasoning_model", "some-openai-model")
    monkeypatch.setattr(settings, "ai_reasoning_effort", "")
    config = resolve_tier(ModelTier.REASONING)
    assert (config.provider, config.model, config.effort, config.refusal_fallback) == ("openai", "some-openai-model", None, False)


def test_cost_estimate():
    assert estimate_cost_usd("claude-opus-5", 1_000_000, 100_000) == pytest.approx(7.5)
    assert estimate_cost_usd("unknown-model", 10, 10) is None


async def test_generate_records_usage(fake_ai):
    fake_ai(lambda *_: Echo(text="hi"))
    records = []

    async def listen(record):
        records.append(record)

    add_usage_listener(listen)
    result = await generate(ModelTier.FAST, system="s", prompt="p", output_type=Echo, task="t", context={"user_id": "u"})
    assert result.output.text == "hi"
    assert records[0].success and records[0].task == "t" and records[0].context == {"user_id": "u"}
    assert records[0].estimated_cost_usd is not None


async def test_failures_are_recorded_and_reraised(fake_ai):
    def refuse(*_):
        raise AIRefusalError("declined")

    fake_ai(refuse)
    records = []

    async def listen(record):
        records.append(record)

    add_usage_listener(listen)
    with pytest.raises(AIRefusalError):
        await generate(ModelTier.FAST, system="s", prompt="p", output_type=Echo, task="t")
    assert not records[0].success and "AIRefusalError" in records[0].error


async def test_broken_usage_listener_never_breaks_the_request(fake_ai):
    fake_ai(lambda *_: Echo(text="ok"))

    async def broken(record):
        raise RuntimeError("sink down")

    add_usage_listener(broken)
    assert (await generate(ModelTier.FAST, system="s", prompt="p", output_type=Echo, task="t")).output.text == "ok"


async def test_unknown_provider_is_a_configuration_error(fake_ai, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "ai_fast_provider", "nonexistent")
    with pytest.raises(AIConfigurationError):
        await generate(ModelTier.FAST, system="s", prompt="p", output_type=Echo, task="t")
