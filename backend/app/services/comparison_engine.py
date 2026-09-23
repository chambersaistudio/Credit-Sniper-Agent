"""
Cross-bureau comparison engine.

Given the bureau-specific records linked to one CanonicalAccount, finds
per-field discrepancies and classifies each with a rule-based first pass —
no LLM call. "A difference between bureaus is NOT automatically an error
or violation" (Phase 1.5 spec), so classification has four levels of
increasing severity:

  DIFFERENCE            — expected/benign (rounding, reporting-date lag)
  POTENTIAL_INCONSISTENCY — worth a second look, not yet well-supported
  LIKELY_INACCURACY      — a rule caught a real contradiction
  SUPPORTED_DISPUTE_GROUND — strong, specific, well-documented basis

Only findings the rules can't confidently classify should go to the
reasoning engine (Stage 2) — this module never itself decides "yes,
dispute this," it only surfaces and grades discrepancies.
"""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class Severity(str, Enum):
    DIFFERENCE = "difference"
    POTENTIAL_INCONSISTENCY = "potential_inconsistency"
    LIKELY_INACCURACY = "likely_inaccuracy"
    SUPPORTED_DISPUTE_GROUND = "supported_dispute_ground"


_SEVERITY_ORDER = {s: i for i, s in enumerate(Severity)}


@dataclass
class ComparisonFinding:
    field: str
    severity: Severity
    values_by_bureau: dict[str, Any]
    rationale: str


_CLOSED_LIKE = {"closed", "paid", "paid in full", "transferred", "sold"}
_NEGATIVE_TERMINAL = {"charged_off", "charge_off", "collection"}
_OPEN_LIKE = {"open", "current"}


def _normalize_status(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().lower().replace(" ", "_")


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%m/%d/%Y", "%m/%Y", "%Y-%m-%d", "%Y-%m", "%m-%Y"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def _compare_balance(records: list[dict[str, Any]]) -> ComparisonFinding | None:
    values = {r["bureau"]: r.get("balance") for r in records if r.get("balance") is not None}
    if len(values) < 2:
        return None
    amounts = list(values.values())
    spread = max(amounts) - min(amounts)
    if spread == 0:
        return None

    statuses = {_normalize_status(r.get("account_status")) for r in records}
    all_terminal = statuses and statuses.issubset(_CLOSED_LIKE | _NEGATIVE_TERMINAL)

    if spread <= 1.0:
        severity = Severity.DIFFERENCE
        rationale = "Balances differ by $1 or less — consistent with rounding."
    elif all_terminal:
        severity = Severity.LIKELY_INACCURACY
        rationale = (
            "Balances differ by more than $1 across bureaus even though every "
            "bureau reports the account as closed/charged-off/collection — a "
            "closed account's balance shouldn't still be moving."
        )
    else:
        severity = Severity.POTENTIAL_INCONSISTENCY
        rationale = "Balances differ across bureaus; at least one account is still open, so this may reflect reporting-date timing."

    return ComparisonFinding("balance", severity, values, rationale)


def _compare_account_status(records: list[dict[str, Any]]) -> ComparisonFinding | None:
    values = {r["bureau"]: r.get("account_status") for r in records if r.get("account_status")}
    normalized = {b: _normalize_status(v) for b, v in values.items()}
    if len(set(normalized.values())) < 2:
        return None

    statuses = set(normalized.values())
    contradiction = (statuses & _OPEN_LIKE) and (statuses & (_CLOSED_LIKE | _NEGATIVE_TERMINAL))
    severity = Severity.LIKELY_INACCURACY if contradiction else Severity.POTENTIAL_INCONSISTENCY
    rationale = (
        "One bureau reports this account open/current while another reports it closed, "
        "charged-off, or in collection for the same tradeline."
        if contradiction
        else "Account status wording differs across bureaus without a clear open/closed contradiction."
    )
    return ComparisonFinding("account_status", severity, values, rationale)


def _compare_dofd(records: list[dict[str, Any]]) -> ComparisonFinding | None:
    values = {r["bureau"]: r.get("date_of_first_delinquency") for r in records if r.get("date_of_first_delinquency")}
    if len(values) < 2:
        return None
    parsed = {b: _parse_date(v) for b, v in values.items()}
    parsed = {b: d for b, d in parsed.items() if d is not None}
    if len(parsed) < 2:
        return None

    dates = list(parsed.values())
    max_gap_days = max((d1 - d2).days for d1 in dates for d2 in dates)

    if max_gap_days <= 30:
        return None
    if max_gap_days > 90:
        severity = Severity.SUPPORTED_DISPUTE_GROUND
        rationale = (
            f"Date of First Delinquency differs by {max_gap_days} days across bureaus. "
            "DOFD controls the FCRA 7-year reporting clock (15 U.S.C. § 1681c), so a gap "
            "this large is a specific, well-documented basis for dispute, not just a difference."
        )
    else:
        severity = Severity.POTENTIAL_INCONSISTENCY
        rationale = f"Date of First Delinquency differs by {max_gap_days} days across bureaus."
    return ComparisonFinding("date_of_first_delinquency", severity, values, rationale)


def _compare_payment_status(records: list[dict[str, Any]]) -> ComparisonFinding | None:
    values = {r["bureau"]: r.get("payment_status") for r in records if r.get("payment_status")}
    normalized = {b: _normalize_status(v) for b, v in values.items()}
    if len(set(normalized.values())) < 2:
        return None
    return ComparisonFinding(
        "payment_status",
        Severity.POTENTIAL_INCONSISTENCY,
        values,
        "Payment status differs across bureaus. Bureau vocabulary for this field varies "
        "enough on its own that this needs closer review rather than an automatic rule.",
    )


_FIELD_COMPARATORS = [_compare_balance, _compare_account_status, _compare_dofd, _compare_payment_status]


def compare_canonical_account(bureau_records: list[dict[str, Any]]) -> list[ComparisonFinding]:
    """
    bureau_records: one dict per bureau, each with at least "bureau" and
    whichever of balance/account_status/payment_status/
    date_of_first_delinquency are known. Records from only one bureau
    produce no findings — there's nothing to compare yet.
    """
    if len(bureau_records) < 2:
        return []
    findings = [f for comparator in _FIELD_COMPARATORS if (f := comparator(bureau_records)) is not None]
    return sorted(findings, key=lambda f: _SEVERITY_ORDER[f.severity], reverse=True)
