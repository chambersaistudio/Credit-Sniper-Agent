"""Offline benchmarking of the document-extraction pipeline."""
from app.services.benchmark.configs import (
    CONFIGS, ESCALATION_CONFIG, BenchmarkConfig, config_by_name, tier_overrides,
)
from app.services.benchmark.groundtruth import GroundTruth, load_ground_truth
from app.services.benchmark.runner import PassCost, RunResult, run_config, run_suite
from app.services.benchmark.scoring import Scorecard, score_extraction

__all__ = [
    "CONFIGS", "ESCALATION_CONFIG", "BenchmarkConfig", "GroundTruth", "PassCost", "RunResult",
    "Scorecard", "config_by_name", "load_ground_truth", "run_config", "run_suite",
    "score_extraction", "tier_overrides",
]
