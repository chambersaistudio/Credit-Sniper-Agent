"""
Running the benchmark, and what a configuration would cost in production.

Latency, tokens and cost are taken from the pipeline's own usage records — the
same numbers production bills against — rather than re-measured here, so a
benchmark result and a production invoice are commensurable.

Two passes per document per config. Each run is independent: a config that
fails on one document does not stop the suite.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from app.services.ai import UsageRecord, only_usage_listener
from app.services.benchmark.configs import CONFIGS, ESCALATION_CONFIG, BenchmarkConfig, tier_overrides
from app.services.benchmark.groundtruth import GroundTruth, normalize_text, values_match
from app.services.benchmark.scoring import Scorecard, score_extraction
from app.services.document_extraction.pipeline import run_auditor, run_extractor
from app.services.document_extraction.status import ExtractionStatus

logger = logging.getLogger(__name__)


@dataclass
class PassCost:
    pass_name: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    estimated_cost_usd: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pass": self.pass_name, "model": self.model,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "latency_ms": round(self.latency_ms, 1),
            "estimated_cost_usd": self.estimated_cost_usd, "error": self.error,
        }


@dataclass
class RunResult:
    document: str
    config: BenchmarkConfig
    scorecard: Scorecard
    costs: list[PassCost] = field(default_factory=list)
    provider_error: str | None = None
    failure_status: str | None = None

    @property
    def total_cost_usd(self) -> float | None:
        known = [c.estimated_cost_usd for c in self.costs if c.estimated_cost_usd is not None]
        return round(sum(known), 6) if known else None

    @property
    def total_latency_ms(self) -> float:
        return round(sum(c.latency_ms for c in self.costs), 1)

    @property
    def total_tokens(self) -> int:
        return sum(c.input_tokens + c.output_tokens for c in self.costs)

    @property
    def verified(self) -> bool:
        return self.scorecard.final_status == ExtractionStatus.VERIFIED.value

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.scorecard.to_dict(),
            "config_label": self.config.label,
            "cost": {
                "passes": [c.to_dict() for c in self.costs],
                "total_usd": self.total_cost_usd,
                "total_tokens": self.total_tokens,
                "total_latency_ms": self.total_latency_ms,
            },
            "provider_error": self.provider_error,
            "failure_status": self.failure_status,
        }


class _UsageCollector:
    """Captures the pipeline's own usage records for the passes we run."""

    def __init__(self):
        self.records: list[UsageRecord] = []

    async def __call__(self, record: UsageRecord) -> None:
        self.records.append(record)

    def cost_for(self, task: str, pass_name: str) -> PassCost:
        record = next((r for r in reversed(self.records) if r.task == task), None)
        if record is None:
            return PassCost(pass_name=pass_name, error="no usage recorded")
        return PassCost(
            pass_name=pass_name, model=record.model,
            input_tokens=record.input_tokens or 0, output_tokens=record.output_tokens or 0,
            latency_ms=record.latency_ms or 0.0,
            estimated_cost_usd=record.estimated_cost_usd,
            error=record.error,
        )


async def run_config(document: bytes, truth: GroundTruth, config: BenchmarkConfig) -> RunResult:
    """Both passes over one document under one configuration."""
    collector = _UsageCollector()
    # Usage here is measurement of a candidate configuration, so it goes to
    # the collector alone and never to the app's usage table.
    with only_usage_listener(collector), tier_overrides(config):
        extraction, _, extract_error = await run_extractor(
            document, filename=f"{truth.name}.pdf", context={"benchmark": config.name}
        )
        audit, audit_error = None, None
        if extraction is not None:
            audit, _, audit_error = await run_auditor(
                document, extraction, filename=f"{truth.name}.pdf",
                context={"benchmark": config.name},
            )

    costs = [collector.cost_for("extract_report_document", "extract")]
    if extraction is not None:
        costs.append(collector.cost_for("audit_report_document", "audit"))

    failure = extract_error or audit_error
    return RunResult(
        document=truth.name, config=config,
        scorecard=score_extraction(truth, config.name, extraction, audit),
        costs=costs,
        # Operator text, including the classified failure: a config that blows
        # the token budget must be distinguishable in the results from one the
        # provider simply refused to serve.
        provider_error=(f"{failure.error_class}: {failure.detail}" if failure else None),
        failure_status=(failure.status.value if failure else None),
    )


# ── Escalation policy ───────────────────────────────────────────────────
# Sol is not a cheap default. In production a candidate config runs first, and
# Sol runs only when the two cheap configs disagree about the document or when
# the quality gate refuses the cheap reading. This simulates that policy over
# the benchmark's own runs, so the cost comparison is the cost of the POLICY
# rather than of one model in isolation.

# Fields whose disagreement means the two configs read the document
# differently enough that we don't know which to trust.
MATERIAL_FIELDS = ("creditor_name", "original_creditor", "account_number", "balance",
                   "open_closed", "status_raw", "past_due_amount", "date_opened")


def configs_disagree(first: Scorecard, second: Scorecard) -> list[str]:
    """Why two configs' readings of the same document can't both be relied on."""
    reasons = []
    if first.extracted_accounts != second.extracted_accounts:
        reasons.append(
            f"tradeline count differs ({first.extracted_accounts} vs {second.extracted_accounts})"
        )
    if first.bureau_correct != second.bureau_correct:
        reasons.append("bureau identification differs")
    if first.score_correct != second.score_correct:
        reasons.append("credit score differs")
    if sorted(first.missing_accounts or []) != sorted(second.missing_accounts or []):
        reasons.append("different accounts missing")
    for name in MATERIAL_FIELDS:
        a, b = first.per_field.get(name), second.per_field.get(name)
        if a and b and a["correct"] != b["correct"]:
            reasons.append(f"{name} read differently")
    return reasons


def gate_failed(scorecard: Scorecard) -> bool:
    return scorecard.final_status != ExtractionStatus.VERIFIED.value


@dataclass
class PolicyOutcome:
    document: str
    candidate: str
    escalated: bool
    reasons: list[str] = field(default_factory=list)
    candidate_cost_usd: float | None = None
    escalation_cost_usd: float | None = None
    final_status: str = ExtractionStatus.FAILED.value

    @property
    def total_cost_usd(self) -> float | None:
        parts = [c for c in (self.candidate_cost_usd, self.escalation_cost_usd) if c is not None]
        return round(sum(parts), 6) if parts else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "document": self.document, "candidate": self.candidate, "escalated": self.escalated,
            "reasons": self.reasons, "candidate_cost_usd": self.candidate_cost_usd,
            "escalation_cost_usd": self.escalation_cost_usd, "total_cost_usd": self.total_cost_usd,
            "final_status": self.final_status,
        }


def simulate_policy(candidate: str, results: dict[tuple[str, str], RunResult],
                    documents: list[str]) -> list[PolicyOutcome]:
    """What "run <candidate>, escalate to Sol when needed" would have cost and
    produced, using only runs the benchmark already performed."""
    outcomes = []
    escalation = ESCALATION_CONFIG.name
    for document in documents:
        run = results.get((document, candidate))
        if run is None:
            continue
        other = "B" if candidate == "A" else "A"
        peer = results.get((document, other))
        reasons = []
        if gate_failed(run.scorecard):
            reasons.append(f"quality gate: {run.scorecard.final_status}")
        if peer is not None:
            reasons += configs_disagree(run.scorecard, peer.scorecard)

        escalated = bool(reasons)
        sol = results.get((document, escalation))
        outcomes.append(PolicyOutcome(
            document=document, candidate=candidate, escalated=escalated, reasons=reasons,
            candidate_cost_usd=run.total_cost_usd,
            escalation_cost_usd=(sol.total_cost_usd if escalated and sol else None),
            final_status=(sol.scorecard.final_status if escalated and sol else run.scorecard.final_status),
        ))
    return outcomes


async def run_suite(
    truths: list[tuple[GroundTruth, bytes]], configs=CONFIGS, *, concurrency: int = 1
) -> dict[str, Any]:
    """Run every config over every document and report the comparison.

    Serial by default: latency is one of the measurements, and concurrent runs
    against one provider distort it."""
    results: dict[tuple[str, str], RunResult] = {}
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def one(truth: GroundTruth, document: bytes, config: BenchmarkConfig):
        async with semaphore:
            logger.info("benchmark: %s under %s", truth.name, config.label)
            results[(truth.name, config.name)] = await run_config(document, truth, config)

    for truth, document in truths:
        for config in configs:
            await one(truth, document, config)

    documents = [truth.name for truth, _ in truths]
    return {
        "documents": documents,
        "configs": [{"name": c.name, "label": c.label, "baseline": c.baseline, "note": c.note}
                    for c in configs],
        "runs": [results[key].to_dict() for key in sorted(results)],
        "totals": {config.name: _totals([results[(d, config.name)] for d in documents
                                         if (d, config.name) in results])
                   for config in configs},
        "policies": {
            candidate: [o.to_dict() for o in simulate_policy(candidate, results, documents)]
            for candidate in [c.name for c in configs if not c.baseline]
        },
        "policy_totals": {
            candidate: _policy_totals(simulate_policy(candidate, results, documents))
            for candidate in [c.name for c in configs if not c.baseline]
        },
    }


def _totals(runs: list[RunResult]) -> dict[str, Any]:
    if not runs:
        return {}
    costs = [r.total_cost_usd for r in runs if r.total_cost_usd is not None]
    fields_correct = sum(r.scorecard.fields.correct for r in runs)
    fields_total = sum(r.scorecard.fields.total for r in runs)
    history_correct = sum(r.scorecard.payment_history.correct for r in runs)
    history_total = sum(r.scorecard.payment_history.total for r in runs)
    return {
        "documents": len(runs),
        "verified": sum(1 for r in runs if r.verified),
        "account_count_exact": sum(1 for r in runs if r.scorecard.account_count_correct),
        "bureau_correct": sum(1 for r in runs if r.scorecard.bureau_correct),
        "field_accuracy": round(fields_correct / fields_total, 4) if fields_total else None,
        "payment_history_accuracy": round(history_correct / history_total, 4) if history_total else None,
        "provenance_accuracy": _ratio(runs, lambda s: (s.provenance.correct, s.provenance.total)),
        "inquiry_accuracy": _ratio(runs, lambda s: (s.inquiries.correct, s.inquiries.total)),
        "hard_inquiries_overcounted": sum(r.scorecard.hard_inquiries_overcounted for r in runs),
        "audit_false_positives": sum(r.scorecard.audit_false_positive_count for r in runs),
        "missing_accounts": sum(len(r.scorecard.missing_accounts) for r in runs),
        "spurious_accounts": sum(len(r.scorecard.spurious_accounts) for r in runs),
        "total_cost_usd": round(sum(costs), 6) if costs else None,
        "cost_per_report_usd": round(sum(costs) / len(costs), 6) if costs else None,
        "mean_latency_ms": round(sum(r.total_latency_ms for r in runs) / len(runs), 1),
        "total_tokens": sum(r.total_tokens for r in runs),
    }


def _ratio(runs: list[RunResult], pick) -> float | None:
    correct = total = 0
    for run in runs:
        c, t = pick(run.scorecard)
        correct += c
        total += t
    return round(correct / total, 4) if total else None


def _policy_totals(outcomes: list[PolicyOutcome]) -> dict[str, Any]:
    if not outcomes:
        return {}
    costs = [o.total_cost_usd for o in outcomes if o.total_cost_usd is not None]
    return {
        "documents": len(outcomes),
        "escalated": sum(1 for o in outcomes if o.escalated),
        "escalation_rate": round(sum(1 for o in outcomes if o.escalated) / len(outcomes), 4),
        "verified": sum(1 for o in outcomes if o.final_status == ExtractionStatus.VERIFIED.value),
        "total_cost_usd": round(sum(costs), 6) if costs else None,
        "cost_per_report_usd": round(sum(costs) / len(costs), 6) if costs else None,
    }
