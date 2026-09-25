"""
The benchmark harness, run offline against mocked providers.

What is asserted is the harness itself: that a perfect reading scores as
perfect, that a degraded reading is caught in the specific way it degraded,
that an auditor objecting to a correct value is counted as a false positive
rather than a catch, that cost comes from the pipeline's own usage records,
and — the safety property — that running a benchmark leaves production
defaults exactly as it found them.

No network, no database, no provider spend.
"""
import copy
from pathlib import Path

import pytest

from app.config import settings
from app.services.ai import register_provider
from app.services.ai.providers import ProviderResult, _instances
from app.services.benchmark import (
    CONFIGS, ESCALATION_CONFIG, config_by_name, load_ground_truth, run_config, run_suite,
    score_extraction, tier_overrides,
)
from app.services.benchmark.report import render_markdown
from app.services.benchmark.runner import configs_disagree, simulate_policy
from app.services.document_extraction.schema import AuditFinding, AuditReport, CreditReportExtraction
from tests.test_document_ingestion_flow import SOURCE_PDF_TEXT, _pdf, golden_extraction, verifying_audit

FIXTURES = Path(__file__).parent / "fixtures" / "benchmark"
DOCUMENT = _pdf(SOURCE_PDF_TEXT)


@pytest.fixture
def truth():
    return load_ground_truth(FIXTURES / "experian_golden.json")


class BenchmarkProvider:
    """Returns a per-model extraction/audit and records what it was asked."""

    name = "openai"

    def __init__(self, outputs=None, default=None):
        self.outputs = outputs or {}
        self.default = default or (golden_extraction(), verifying_audit())
        self.calls: list[dict] = []

    async def generate(self, *args, **kwargs):
        raise AssertionError("the benchmark must not fall back to the text provider")

    async def generate_document(self, config, *, system, prompt, document, filename, output_type,
                                max_tokens, detail="high"):
        self.calls.append({"model": config.model, "detail": detail, "output_type": output_type})
        extraction, audit = self.outputs.get(config.model, self.default)
        output = extraction if output_type is CreditReportExtraction else audit
        return ProviderResult(
            output=output, provider=self.name, model=config.model,
            input_tokens=30_000, output_tokens=6_000, cache_read_tokens=0, cache_write_tokens=0,
            latency_ms=1234.0,
        )


@pytest.fixture
def provider():
    saved = dict(_instances)
    installed = {}

    def install(outputs=None, default=None):
        installed["provider"] = BenchmarkProvider(outputs, default)
        register_provider("openai", installed["provider"])
        return installed["provider"]

    yield install
    _instances.clear()
    _instances.update(saved)


# ── Scoring ─────────────────────────────────────────────────────────────

async def test_a_perfect_reading_scores_as_perfect(truth, provider):
    provider()
    result = await run_config(DOCUMENT, truth, config_by_name("A"))
    card = result.scorecard

    assert card.expected_accounts == card.extracted_accounts == card.matched_accounts == 15
    assert card.account_count_correct
    assert (card.missing_accounts, card.spurious_accounts) == ([], [])
    assert (card.bureau_correct, card.score_correct, card.score_type_correct) == (True, True, True)
    assert card.report_date_correct is True
    assert card.fields.accuracy == 1.0 and card.fields.total > 100
    assert card.payment_history.accuracy == 1.0
    assert card.inquiries.accuracy == 1.0 and card.inquiry_count_correct
    assert card.provenance.accuracy == 1.0
    assert card.audit_false_positives == []
    assert card.final_status == "verified"
    assert card.hard_inquiries_overcounted == 0


async def test_a_degraded_reading_is_caught_in_the_way_it_degraded(truth, provider):
    """One account dropped, one balance wrong, one soft inquiry called hard —
    each has to show up as its own failure, not as one blended score."""
    # As if this one sat under a "Promotional Inquiries" heading.
    truth.inquiries[1]["inquiry_type"] = "soft"
    truth.inquiries[1]["inquiry_category"] = "promotional"

    degraded = golden_extraction()
    dropped = degraded.accounts.pop(2).creditor_name
    degraded.accounts[0].balance = "$1,600"          # was $16
    degraded.inquiries[1].inquiry_type = "hard"      # the document says soft

    honest_audit = verifying_audit()
    honest_audit.account_count_matches = False       # a real auditor counts 15

    # A audits with terra, so the degraded pair is the default here.
    provider(default=(degraded, honest_audit))
    card = (await run_config(DOCUMENT, truth, config_by_name("A"))).scorecard

    assert card.extracted_accounts == 14
    assert card.missing_accounts == [dropped]
    assert card.spurious_accounts == []
    assert not card.account_count_correct
    assert card.fields.accuracy < 1.0
    assert any(m["field"] == "balance" and m["expected"] == "$16" for m in card.fields.misses)
    assert card.per_field["balance"]["correct"] == card.per_field["balance"]["total"] - 1
    assert card.hard_inquiries_overcounted == 1
    # 14 extracted against an audit that counted 15: incomplete, not verified.
    assert card.final_status == "extraction_incomplete"


async def test_money_and_date_formatting_is_not_scored_as_a_miss(truth, provider):
    """The model transcribes what the report prints. '$1,204' and '1204.00'
    are the same answer, and a benchmark that punished the formatting would
    reward the wrong model."""
    reformatted = golden_extraction()
    for account in reformatted.accounts:
        if account.balance:
            account.balance = account.balance.replace("$", "").replace(",", "") + ".00"
        if account.date_opened:
            account.date_opened = "2025-12-22"

    provider(outputs={"gpt-5.6-luna": (reformatted, verifying_audit())})
    card = (await run_config(DOCUMENT, truth, config_by_name("A"))).scorecard
    assert card.per_field["balance"]["correct"] == card.per_field["balance"]["total"]
    assert card.per_field["date_opened"]["correct"] == card.per_field["date_opened"]["total"]


async def test_an_auditor_objecting_to_a_correct_value_is_a_false_positive(truth, provider):
    """The metric that decides whether a cheaper auditor is usable: objecting
    to a value the document actually prints holds a good report out of dispute
    analysis."""
    audit = AuditReport(
        account_count_in_document=15, account_count_matches=True, identities_correct=True,
        inquiries_correct=True, verified=False, confidence=0.5,
        findings=[AuditFinding(
            kind="correction", account_name="ATLAS", field="balance",
            extracted_value="$16", correct_value="$1,600", page=3,
            explanation="reads $1,600 to me",
        )],
    )
    provider(default=(golden_extraction(), audit))
    card = (await run_config(DOCUMENT, truth, config_by_name("B"))).scorecard

    assert card.audit_blocking == 1
    assert card.audit_true_positives == 0
    assert [(f["account"], f["field"]) for f in card.audit_false_positives] == [("ATLAS", "balance")]
    assert card.final_status != "verified"


async def test_an_auditor_catching_a_real_error_is_a_true_positive(truth, provider):
    wrong = golden_extraction()
    wrong.accounts[0].balance = "$1,600"
    audit = AuditReport(
        account_count_in_document=15, account_count_matches=True, identities_correct=True,
        inquiries_correct=True, verified=False, confidence=0.9,
        findings=[AuditFinding(
            kind="correction", account_name="ATLAS", field="balance",
            extracted_value="$1,600", correct_value="$16", page=3,
            explanation="the document says $16",
        )],
    )
    provider(default=(wrong, audit))
    card = (await run_config(DOCUMENT, truth, config_by_name("B"))).scorecard
    assert card.audit_false_positives == []
    assert card.audit_true_positives == 1


async def test_missing_provenance_is_scored_even_when_the_values_are_right(truth, provider):
    """An extraction nothing can be traced back to isn't auditable later,
    however accurate it looks."""
    unsourced = golden_extraction()
    for account in unsourced.accounts:
        account.source_pages = []
    provider(default=(unsourced, verifying_audit()))
    card = (await run_config(DOCUMENT, truth, config_by_name("A"))).scorecard
    assert card.provenance.accuracy == 0.0
    assert card.fields.accuracy == 1.0
    assert card.final_status != "verified"


async def test_a_provider_failure_scores_as_no_extraction(truth, provider):
    from app.services.ai import AIProviderError

    class Down(BenchmarkProvider):
        async def generate_document(self, *args, **kwargs):
            raise AIProviderError("OpenAI API error 429: insufficient_quota")

    register_provider("openai", Down())
    result = await run_config(DOCUMENT, truth, config_by_name("A"))
    assert result.scorecard.extracted_accounts == 0
    assert result.scorecard.final_status == "failed"
    assert "429" in result.provider_error
    assert result.total_cost_usd is None


# ── Configuration plumbing ──────────────────────────────────────────────

async def test_each_config_reaches_the_provider_as_specified(truth, provider):
    fake = provider()
    for config in CONFIGS:
        await run_config(DOCUMENT, truth, config)

    by_config = [(c["model"], c["detail"]) for c in fake.calls]
    assert by_config == [
        ("gpt-5.6-luna", "low"), ("gpt-5.6-terra", "low"),     # A: cheap extract, mid audit
        ("gpt-5.6-terra", "low"), ("gpt-5.6-terra", "low"),    # B: mid for both
        ("gpt-5.6-sol", "high"), ("gpt-5.6-sol", "high"),      # C: baseline
    ]


async def test_benchmarking_leaves_production_defaults_untouched(truth, provider):
    provider()
    before = copy.deepcopy(settings.model_dump())
    await run_suite([(truth, DOCUMENT)], CONFIGS)
    assert settings.model_dump() == before


def test_tier_overrides_restore_settings_even_when_the_run_raises():
    before = (settings.ai_document_extraction_model, settings.ai_document_audit_model,
              settings.document_extraction_detail)
    with pytest.raises(RuntimeError):
        with tier_overrides(config_by_name("A")):
            assert settings.ai_document_extraction_model == "gpt-5.6-luna"
            raise RuntimeError("boom")
    assert (settings.ai_document_extraction_model, settings.ai_document_audit_model,
            settings.document_extraction_detail) == before


async def test_cost_comes_from_the_pipelines_own_usage_records(truth, provider):
    provider()
    a = await run_config(DOCUMENT, truth, config_by_name("A"))
    c = await run_config(DOCUMENT, truth, config_by_name("C"))

    # 30k in + 6k out per pass, priced per MODEL_PRICING.
    # A: luna 30k×$0.20 + 6k×$1.20, then terra 30k×$2 + 6k×$12.
    assert a.costs[0].estimated_cost_usd == pytest.approx(0.0132, abs=1e-6)
    assert a.costs[1].estimated_cost_usd == pytest.approx(0.132, abs=1e-6)
    assert a.total_cost_usd == pytest.approx(0.1452, abs=1e-6)
    # C: sol both passes, 30k×$4 + 6k×$20 each.
    assert c.total_cost_usd == pytest.approx(0.48, abs=1e-6)
    assert a.total_cost_usd < c.total_cost_usd
    assert a.total_tokens == 72_000
    assert a.total_latency_ms == pytest.approx(2468.0)


async def test_benchmark_usage_never_reaches_the_applications_listeners(truth, provider):
    from app.services.ai import add_usage_listener, clear_usage_listeners

    seen = []

    async def app_sink(record):
        seen.append(record)

    clear_usage_listeners()
    add_usage_listener(app_sink)
    try:
        provider()
        await run_config(DOCUMENT, truth, config_by_name("A"))
        assert seen == [], "benchmark measurements must not be billed as real work"
        # And the app's listener is still installed afterwards.
        from app.services.ai.usage import _listeners
        assert app_sink in _listeners
    finally:
        clear_usage_listeners()


# ── Escalation policy ───────────────────────────────────────────────────

async def test_agreeing_configs_that_pass_the_gate_never_escalate(truth, provider):
    provider()
    result = await run_suite([(truth, DOCUMENT)], CONFIGS)
    policy = result["policy_totals"]["A"]
    assert policy["escalated"] == 0
    assert policy["verified"] == 1
    # The whole point: the policy costs the candidate, not the baseline.
    assert policy["cost_per_report_usd"] < result["totals"]["C"]["cost_per_report_usd"]


async def test_disagreement_between_candidates_escalates_to_sol(truth, provider):
    """Sol is the escalation, not a default: it runs because A and B read the
    document differently, not because it is available."""
    disagreeing = golden_extraction()
    disagreeing.accounts.pop()
    provider(outputs={"gpt-5.6-luna": (disagreeing, verifying_audit())})

    result = await run_suite([(truth, DOCUMENT)], CONFIGS)
    outcome = result["policies"]["A"][0]
    assert outcome["escalated"] is True
    assert any("tradeline count differs" in r for r in outcome["reasons"])
    assert outcome["escalation_cost_usd"] is not None
    # Escalating costs the candidate AND the baseline — that is the trade.
    assert outcome["total_cost_usd"] > result["totals"]["C"]["cost_per_report_usd"]
    # And the escalated reading is the one that counts.
    assert outcome["final_status"] == "verified"


async def test_a_failed_quality_gate_escalates_even_without_disagreement(truth, provider):
    """Both cheap configs agree, and both are wrong in the same way. The gate
    refusing their reading is enough on its own."""
    unsourced = golden_extraction()
    for account in unsourced.accounts:
        account.source_pages = []
    provider(outputs={
        "gpt-5.6-luna": (unsourced, verifying_audit()),
        "gpt-5.6-terra": (unsourced, verifying_audit()),
    })
    result = await run_suite([(truth, DOCUMENT)], CONFIGS)
    outcome = result["policies"]["B"][0]
    assert outcome["escalated"] is True
    assert any(r.startswith("quality gate") for r in outcome["reasons"])
    assert outcome["final_status"] == "verified"


def test_configs_disagree_reports_why_not_merely_that():
    perfect = score_extraction(load_ground_truth(FIXTURES / "experian_golden.json"), "A",
                               golden_extraction(), verifying_audit())
    short = golden_extraction()
    short.accounts.pop()
    other = score_extraction(load_ground_truth(FIXTURES / "experian_golden.json"), "B",
                             short, verifying_audit())
    reasons = configs_disagree(perfect, other)
    assert any("tradeline count differs (15 vs 14)" in r for r in reasons)
    assert configs_disagree(perfect, perfect) == []


def test_the_escalation_target_is_the_baseline():
    assert ESCALATION_CONFIG.baseline
    assert (ESCALATION_CONFIG.extraction_model, ESCALATION_CONFIG.audit_model) == (
        "gpt-5.6-sol", "gpt-5.6-sol")
    assert [c.name for c in CONFIGS if not c.baseline] == ["A", "B"]


async def test_the_report_states_every_measurement_asked_for(truth, provider):
    provider()
    markdown = render_markdown(await run_suite([(truth, DOCUMENT)], CONFIGS))
    for heading in ("Verified", "Account count exact", "Bureau", "Field acc.",
                    "Payment history", "Inquiries", "Provenance", "Audit false pos.",
                    "Cost / report", "Mean latency", "With Sol as escalation only"):
        assert heading in markdown, heading
    for config in CONFIGS:
        assert config.name in markdown
