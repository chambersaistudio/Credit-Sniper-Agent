"""
Cross-bureau comparison engine.

Given the bureau records linked to one CanonicalAccount, finds per-field
discrepancies and grades them with deterministic rules — no LLM call.
Only the reasoning engine decides whether a finding supports a dispute.
"""
from typing import Any, Callable

from app.services.findings import (
    CLOSED_STATUSES,
    NEGATIVE_STATUSES,
    OPEN_STATUSES,
    Finding,
    Severity,
    normalize_status,
    sort_by_severity,
)
from app.utils.dates import parse_report_date

# Differences at or below this are rounding / reporting-cycle noise.
BALANCE_TOLERANCE = 1.00
# Bureaus commonly report the same event a few weeks apart.
DATE_TOLERANCE_DAYS = 30
# A DOFD gap beyond this materially moves the § 1681c reporting period.
DOFD_MATERIAL_GAP_DAYS = 90
OPEN_DATE_TOLERANCE_DAYS = 60

# Statuses where the balance should no longer be moving month to month.
TERMINAL_STATUSES = CLOSED_STATUSES | {"charged_off", "collection"}


def _values(records: list[dict[str, Any]], field: str) -> dict[str, Any]:
    return {r["bureau"]: r.get(field) for r in records if r.get(field) not in (None, "")}


def _compare_balance(records: list[dict[str, Any]]) -> Finding | None:
    values = _values(records, "balance")
    if len(values) < 2:
        return None
    spread = max(values.values()) - min(values.values())
    if spread == 0:
        return None

    statuses = {normalize_status(r.get("account_status")) for r in records} - {None}
    all_terminal = bool(statuses) and statuses <= TERMINAL_STATUSES

    if spread <= BALANCE_TOLERANCE:
        severity, rationale = Severity.DIFFERENCE, "Balances differ by $1 or less — consistent with rounding."
    elif all_terminal:
        severity = Severity.LIKELY_INACCURACY
        rationale = (
            f"Balances differ by ${spread:,.2f} although every bureau reports the account as "
            "closed, charged off, or in collection — a balance that is no longer changing should "
            "not differ between bureaus."
        )
    else:
        severity = Severity.POTENTIAL_INCONSISTENCY
        rationale = (
            f"Balances differ by ${spread:,.2f}. The account is still active on at least one bureau, "
            "so this may be reporting-date timing; compare the date each bureau last updated it."
        )
    return Finding("cross_bureau.balance", "balance", severity, rationale, values)


def _compare_account_status(records: list[dict[str, Any]]) -> Finding | None:
    values = _values(records, "account_status")
    normalized = {normalize_status(v) for v in values.values()}
    if len(normalized) < 2:
        return None
    contradiction = bool(normalized & OPEN_STATUSES) and bool(normalized & (CLOSED_STATUSES | NEGATIVE_STATUSES))
    if contradiction:
        return Finding(
            "cross_bureau.account_status", "account_status", Severity.LIKELY_INACCURACY,
            "One bureau reports this account open/current while another reports it closed, "
            "charged off, or in collection.",
            values,
        )
    return Finding(
        "cross_bureau.account_status", "account_status", Severity.POTENTIAL_INCONSISTENCY,
        "Account status differs across bureaus without a clear open/closed contradiction.",
        values,
    )


def _max_date_gap(values: dict[str, Any]) -> int | None:
    dates = [d for d in (parse_report_date(str(v)) for v in values.values()) if d]
    if len(dates) < 2:
        return None
    return (max(dates) - min(dates)).days


def _compare_dofd(records: list[dict[str, Any]]) -> Finding | None:
    values = _values(records, "date_of_first_delinquency")
    gap = _max_date_gap(values)
    if gap is None or gap <= DATE_TOLERANCE_DAYS:
        return None
    if gap > DOFD_MATERIAL_GAP_DAYS:
        return Finding(
            "cross_bureau.dofd", "date_of_first_delinquency", Severity.SUPPORTED_DISPUTE_GROUND,
            f"Date of First Delinquency differs by {gap} days across bureaus. DOFD sets the end of "
            "the reporting period for adverse items (15 U.S.C. § 1681c(a), (c)), so at most one of "
            "these dates can be accurate and the later one would extend reporting.",
            values,
        )
    return Finding(
        "cross_bureau.dofd", "date_of_first_delinquency", Severity.POTENTIAL_INCONSISTENCY,
        f"Date of First Delinquency differs by {gap} days across bureaus.",
        values,
    )


def _compare_date_opened(records: list[dict[str, Any]]) -> Finding | None:
    values = _values(records, "date_opened")
    gap = _max_date_gap(values)
    if gap is None or gap <= OPEN_DATE_TOLERANCE_DAYS:
        return None
    return Finding(
        "cross_bureau.date_opened", "date_opened", Severity.POTENTIAL_INCONSISTENCY,
        f"Date opened differs by {gap} days across bureaus.",
        values,
    )


def _compare_payment_status(records: list[dict[str, Any]]) -> Finding | None:
    values = _values(records, "payment_status")
    if len({normalize_status(v) for v in values.values()}) < 2:
        return None
    return Finding(
        "cross_bureau.payment_status", "payment_status", Severity.POTENTIAL_INCONSISTENCY,
        "Payment status differs across bureaus. Bureau wording for this field varies, so it "
        "needs review against the month-by-month payment history rather than an automatic rule.",
        values,
    )


def _compare_credit_limit(records: list[dict[str, Any]]) -> Finding | None:
    values = _values(records, "credit_limit")
    if len(values) < 2 or max(values.values()) - min(values.values()) <= BALANCE_TOLERANCE:
        return None
    return Finding(
        "cross_bureau.credit_limit", "credit_limit", Severity.POTENTIAL_INCONSISTENCY,
        "Credit limit differs across bureaus, which changes reported utilization on the lower one.",
        values,
    )


_COMPARATORS: list[Callable[[list[dict[str, Any]]], Finding | None]] = [
    _compare_balance,
    _compare_account_status,
    _compare_dofd,
    _compare_date_opened,
    _compare_payment_status,
    _compare_credit_limit,
]


def compare_bureau_records(bureau_records: list[dict[str, Any]]) -> list[Finding]:
    """One dict per bureau (key "bureau" plus any known account fields).
    Fewer than two bureaus means there's nothing to compare."""
    if len({r["bureau"] for r in bureau_records}) < 2:
        return []
    return sort_by_severity([f for compare in _COMPARATORS if (f := compare(bureau_records)) is not None])
