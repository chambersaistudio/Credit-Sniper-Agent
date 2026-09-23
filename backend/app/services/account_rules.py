"""
Deterministic checks on a single bureau's record for one account.

These replace sending whole reports to an LLM for things plain rules can
decide: whether an item is negative, whether it has outlived the FCRA
reporting period, and whether its dates are internally impossible.
"""
from datetime import date, timedelta
from typing import Any

from app.services.findings import NEGATIVE_STATUSES, Finding, Severity, normalize_status, sort_by_severity
from app.utils.dates import parse_report_date

# 15 U.S.C. § 1681c(a)(4)-(5), (c)(1): adverse items may be reported for 7
# years, measured from 180 days after the delinquency began.
REPORTING_PERIOD_YEARS = 7
DELINQUENCY_GRACE_DAYS = 180

_LATE_PAYMENT_WORDS = ("late", "past due", "delinquen", "charge", "collection")


def is_negative(record: dict[str, Any]) -> bool:
    if normalize_status(record.get("account_status")) in NEGATIVE_STATUSES:
        return True
    payment_status = (record.get("payment_status") or "").lower()
    return any(word in payment_status for word in _LATE_PAYMENT_WORDS)


def reporting_period_end(dofd: date) -> date:
    start = dofd + timedelta(days=DELINQUENCY_GRACE_DAYS)
    try:
        return start.replace(year=start.year + REPORTING_PERIOD_YEARS)
    except ValueError:  # Feb 29 -> Feb 28
        return start.replace(year=start.year + REPORTING_PERIOD_YEARS, day=28)


def evaluate_record(record: dict[str, Any], as_of: date) -> list[Finding]:
    """`record` needs "bureau" plus whichever account fields are known;
    `as_of` is the report date (or today when the report has none)."""
    bureau = record.get("bureau")
    findings: list[Finding] = []
    negative = is_negative(record)

    dofd = parse_report_date(record.get("date_of_first_delinquency"))
    opened = parse_report_date(record.get("date_opened"))
    closed = parse_report_date(record.get("date_closed"))

    if negative and dofd:
        end = reporting_period_end(dofd)
        if as_of > end:
            findings.append(Finding(
                "record.obsolete_reporting", "date_of_first_delinquency", Severity.SUPPORTED_DISPUTE_GROUND,
                f"Adverse account still reporting as of {as_of.isoformat()}, but its reporting period "
                f"ended {end.isoformat()} (7 years from 180 days after the {dofd.isoformat()} Date of "
                "First Delinquency, 15 U.S.C. § 1681c(a), (c)). The § 1681c(b) exemptions (credit "
                "transactions of $150,000+, employment at $75,000+) would need to be ruled out.",
                {"date_of_first_delinquency": dofd.isoformat(), "reporting_period_end": end.isoformat(),
                 "as_of": as_of.isoformat()},
                bureau,
            ))

    if negative and not dofd:
        findings.append(Finding(
            "record.missing_dofd", "date_of_first_delinquency", Severity.POTENTIAL_INCONSISTENCY,
            "Adverse account shows no Date of First Delinquency, so its reporting period can't be "
            "verified from this report. Consumer disclosures sometimes omit the field even when the "
            "furnisher reports it, so this is a question to ask, not yet an error.",
            {"account_status": record.get("account_status")},
            bureau,
        ))

    if dofd and opened and dofd < opened:
        findings.append(Finding(
            "record.dofd_before_opened", "date_of_first_delinquency", Severity.LIKELY_INACCURACY,
            f"Date of First Delinquency ({dofd.isoformat()}) is earlier than the date the account "
            f"was opened ({opened.isoformat()}), which is impossible for the same account.",
            {"date_of_first_delinquency": dofd.isoformat(), "date_opened": opened.isoformat()},
            bureau,
        ))

    if closed and opened and closed < opened:
        findings.append(Finding(
            "record.closed_before_opened", "date_closed", Severity.LIKELY_INACCURACY,
            f"Date closed ({closed.isoformat()}) is earlier than date opened ({opened.isoformat()}).",
            {"date_closed": closed.isoformat(), "date_opened": opened.isoformat()},
            bureau,
        ))

    return sort_by_severity(findings)
