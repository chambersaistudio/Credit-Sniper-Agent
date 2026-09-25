"""
The ground truth a benchmark run is scored against: what a human confirmed the
PDF actually says.

The format is deliberately partial-friendly. Only the fields a truth file
states are scored, so a golden set can start with the things that matter most
(identity, balances, status, dates) and grow, without every unstated field
counting as a miss.

No consumer PII belongs in a truth file. Account numbers are the masked
strings the report prints; names and addresses of the consumer are not scored
and must not be recorded here.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.utils.dates import parse_report_date

# Account fields worth scoring, in the extraction's own vocabulary.
SCORED_FIELDS: tuple[str, ...] = (
    "creditor_name", "original_creditor", "sold_to", "account_number", "account_type",
    "open_closed", "status_raw", "payment_status", "report_classification",
    "balance", "credit_limit", "original_amount", "past_due_amount", "high_balance",
    "monthly_payment", "terms", "responsibility",
    "date_opened", "date_closed", "balance_updated", "date_last_reported",
    "date_last_payment", "date_first_delinquency", "remarks",
)
MONEY_FIELDS = frozenset({
    "balance", "credit_limit", "original_amount", "past_due_amount", "high_balance", "monthly_payment",
})
DATE_FIELDS = frozenset({
    "date_opened", "date_closed", "balance_updated", "date_last_reported",
    "date_last_payment", "date_first_delinquency",
})
INQUIRY_FIELDS: tuple[str, ...] = ("inquiry_date", "inquiry_type", "inquiry_category", "business_type")


@dataclass
class GroundTruth:
    """One document's confirmed contents."""

    name: str
    pdf_path: Path | None = None
    bureau: str | None = None
    report_date: str | None = None
    score: int | None = None
    score_type: str | None = None
    accounts: list[dict[str, Any]] = field(default_factory=list)
    inquiries: list[dict[str, Any]] = field(default_factory=list)
    # Pages each account should be sourced from, when the truth records them.
    expects_provenance: bool = True

    @property
    def account_count(self) -> int:
        return len(self.accounts)


def load_ground_truth(path: str | Path) -> GroundTruth:
    path = Path(path)
    data = json.loads(path.read_text())
    pdf = data.get("pdf")
    return GroundTruth(
        name=data.get("name") or path.stem,
        # Relative to the truth file, so a golden set is movable as a folder.
        pdf_path=(path.parent / pdf).resolve() if pdf else None,
        bureau=(data.get("bureau") or "").strip().lower() or None,
        report_date=data.get("report_date"),
        score=data.get("score"),
        score_type=data.get("score_type"),
        accounts=data.get("accounts") or [],
        inquiries=data.get("inquiries") or [],
        expects_provenance=data.get("expects_provenance", True),
    )


# ── Value comparison ────────────────────────────────────────────────────
# The model transcribes what the report prints, so "$1,204" and "1204.00" are
# the same answer and must not score as a miss. Normalization is deterministic
# and identical to what ingestion does with the value.

_MONEY = re.compile(r"[^0-9.\-]")


def normalize_money(value: Any) -> float | None:
    if value is None:
        return None
    text = _MONEY.sub("", str(value))
    if text in ("", "-", ".", "-."):
        return None
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip().lower()
    return text or None


def normalize_date(value: Any) -> str | None:
    parsed = parse_report_date(value)
    return parsed.isoformat() if parsed else normalize_text(value)


def values_match(field_name: str, expected: Any, actual: Any) -> bool:
    if field_name in MONEY_FIELDS:
        return normalize_money(expected) == normalize_money(actual)
    if field_name in DATE_FIELDS:
        return normalize_date(expected) == normalize_date(actual)
    return normalize_text(expected) == normalize_text(actual)


def account_key(creditor_name: Any, account_number: Any) -> tuple[str, str]:
    """How an extracted tradeline is matched to its ground truth.

    Creditor name plus the last four digits of the masked number. TransUnion
    warns masked numbers can be scrambled, so the digits alone are never the
    key — and neither is the name alone, since a report can list the same
    furnisher twice."""
    digits = re.sub(r"\D", "", str(account_number or ""))
    return (normalize_text(creditor_name) or "", digits[-4:] if len(digits) >= 4 else digits)
