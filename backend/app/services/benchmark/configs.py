"""
The configurations under test, and how to point the pipeline at one.

Production defaults are NOT changed by anything here. A config is applied only
for the duration of a benchmark run, by a context manager that restores the
previous settings — and the harness is a CLI, never the API process.
"""
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class BenchmarkConfig:
    name: str
    extraction_model: str
    audit_model: str
    detail: str
    # A baseline is the reference the cheaper configs are scored against, and
    # the escalation target — not a candidate default.
    baseline: bool = False
    note: str = ""

    @property
    def label(self) -> str:
        return f"{self.name} ({self.extraction_model} / {self.audit_model} / detail={self.detail})"


# Exactly the configurations asked for. A extracts with the cheapest model and
# audits with the mid one; B uses the mid model for both; C is today's
# production pair at full detail and is the reference, used in production only
# as an escalation target.
CONFIG_A = BenchmarkConfig(
    name="A", extraction_model="gpt-5.6-luna", audit_model="gpt-5.6-terra", detail="low",
    note="cheapest extractor, mid auditor",
)
CONFIG_B = BenchmarkConfig(
    name="B", extraction_model="gpt-5.6-terra", audit_model="gpt-5.6-terra", detail="low",
    note="mid model for both passes",
)
CONFIG_C = BenchmarkConfig(
    name="C", extraction_model="gpt-5.6-sol", audit_model="gpt-5.6-sol", detail="high",
    baseline=True, note="current production pair; reference and escalation target",
)

CONFIGS: tuple[BenchmarkConfig, ...] = (CONFIG_A, CONFIG_B, CONFIG_C)
# Sol is the escalation, never a cheap default: it runs when A and B disagree
# or when the quality gate refuses their reading.
ESCALATION_CONFIG = CONFIG_C


def config_by_name(name: str) -> BenchmarkConfig:
    for config in CONFIGS:
        if config.name.lower() == name.lower():
            return config
    raise KeyError(f"unknown benchmark config {name!r}; expected one of {[c.name for c in CONFIGS]}")


@contextmanager
def tier_overrides(config: BenchmarkConfig):
    """Point the document tiers at this config, then put them back.

    Uses the same env-override fields a deployment would set, so a benchmark
    result is reproducible by configuration alone — no code path exists that
    only the benchmark can reach."""
    from app.config import settings

    fields = {
        "ai_document_extraction_model": config.extraction_model,
        "ai_document_audit_model": config.audit_model,
        "document_extraction_detail": config.detail,
        # Both passes must run: the audit is half of what is being measured.
        "document_audit_enabled": True,
        "document_extraction_mode": "on",
    }
    saved = {name: getattr(settings, name) for name in fields}
    for name, value in fields.items():
        setattr(settings, name, value)
    try:
        yield config
    finally:
        for name, value in saved.items():
            setattr(settings, name, value)
