"""
Shared shape for deterministic findings, whether they come from comparing
one tradeline across bureaus (comparison_engine) or checking a single
bureau record on its own (account_rules). Findings are evidence inputs for
the reasoning engine — never themselves a decision to dispute.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """Increasing strength. A difference is never automatically an error."""

    DIFFERENCE = "difference"
    POTENTIAL_INCONSISTENCY = "potential_inconsistency"
    LIKELY_INACCURACY = "likely_inaccuracy"
    SUPPORTED_DISPUTE_GROUND = "supported_dispute_ground"

    @property
    def rank(self) -> int:
        return list(Severity).index(self)


@dataclass
class Finding:
    rule: str  # stable id, e.g. "cross_bureau.balance", "record.obsolete_reporting"
    field: str
    severity: Severity
    rationale: str
    values: dict[str, Any] = field(default_factory=dict)  # bureau -> value, or named values for single-record rules
    bureau: str | None = None  # set for single-record findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "field": self.field,
            "severity": self.severity.value,
            "rationale": self.rationale,
            "values": self.values,
            "bureau": self.bureau,
        }


def sort_by_severity(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: f.severity.rank, reverse=True)


# Normalized account_status values produced by the parser (pdf_parser.STATUS_MAP).
NEGATIVE_STATUSES = {
    "charged_off", "collection", "derogatory", "late",
    "30d_late", "60d_late", "90d_late", "120d_late",
}
CLOSED_STATUSES = {"closed", "paid", "transferred", "sold"}
OPEN_STATUSES = {"open", "current"}


def normalize_status(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().lower().replace(" ", "_")
