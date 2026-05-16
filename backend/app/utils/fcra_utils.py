"""
FCRA utility functions — legal deadline calculations and compliance helpers.
"""
from datetime import datetime, timezone, timedelta

# FCRA statutory references
FCRA_SECTIONS = {
    "reinvestigation": "15 U.S.C. § 1681i",
    "accuracy": "15 U.S.C. § 1681e(b)",
    "obsolete_info": "15 U.S.C. § 1681c",
    "furnisher_accuracy": "15 U.S.C. § 1681s-2",
    "willful_noncompliance": "15 U.S.C. § 1681n",
    "negligent_noncompliance": "15 U.S.C. § 1681o",
    "permissible_purpose": "15 U.S.C. § 1681b",
    "disclosure": "15 U.S.C. § 1681g",
    "notification": "15 U.S.C. § 1681m",
}

METRO2_FIELDS = {
    "17A": "Account Status Code — must accurately reflect current status",
    "17B": "Payment Rating — must reflect actual payment performance",
    "18": "Payment History Profile — 24-month history must be accurate",
    "20": "Scheduled Monthly Payment Amount",
    "23": "Amount Past Due — must be exact",
    "26": "Date of First Delinquency (DOFD) — most critical field; controls 7-year clock",
    "27": "Date Closed — required when account is closed",
    "35": "Compliance Condition Code — required for specific account conditions",
    "36": "Original Charge-Off Amount",
    "K4": "Balloon Payment Amount",
}

REPORTING_LIMITS = {
    "negative_items": 7,           # Years from DOFD for most negative items
    "chapter_7_bankruptcy": 10,    # Years from filing date
    "chapter_13_bankruptcy": 7,    # Years from filing date
    "unpaid_tax_liens": 7,         # Years from filing date
    "civil_judgments": 7,          # Years from date of entry
}


def calculate_statute_of_limitations(dofd: datetime, item_type: str = "negative_items") -> datetime:
    """Calculate when a credit item must be removed from report."""
    years = REPORTING_LIMITS.get(item_type, 7)
    return dofd + timedelta(days=years * 365.25)


def is_past_reporting_limit(dofd: datetime, item_type: str = "negative_items") -> bool:
    """Check if an item has exceeded its FCRA reporting time limit."""
    return datetime.now(timezone.utc) > calculate_statute_of_limitations(dofd, item_type)


def calculate_reinvestigation_deadline(dispute_date: datetime) -> datetime:
    """Calculate bureau's 30-day reinvestigation deadline per 15 U.S.C. § 1681i(a)(1)."""
    return dispute_date + timedelta(days=30)


def calculate_extended_reinvestigation_deadline(dispute_date: datetime) -> datetime:
    """Calculate 45-day extended deadline when consumer provides additional info."""
    return dispute_date + timedelta(days=45)


def has_reinvestigation_deadline_passed(dispute_date: datetime, extended: bool = False) -> bool:
    """Check if bureau has exceeded reinvestigation deadline."""
    deadline = calculate_extended_reinvestigation_deadline(dispute_date) if extended else calculate_reinvestigation_deadline(dispute_date)
    return datetime.now(timezone.utc) > deadline


def get_statutory_damages_range() -> dict:
    """Return FCRA statutory damage ranges for violation notices."""
    return {
        "willful_per_violation": {"min": 100, "max": 1000, "citation": "15 U.S.C. § 1681n(a)(1)(A)"},
        "punitive": "At court's discretion — § 1681n(a)(2)",
        "attorney_fees": "Recoverable for both willful and negligent violations",
        "actual_damages": "Provable actual damages — § 1681o",
    }


def format_fcra_deadline_warning(dispute_date: datetime) -> str:
    """Generate FCRA deadline warning language for letters."""
    deadline = calculate_reinvestigation_deadline(dispute_date)
    return (
        f"Pursuant to 15 U.S.C. § 1681i(a)(1), you are required to complete "
        f"your reinvestigation within 30 days of receipt of this dispute, "
        f"no later than {deadline.strftime('%B %d, %Y')}. Failure to do so "
        f"within the statutory timeframe may constitute a willful violation of "
        f"the FCRA, subjecting you to statutory damages of $100 to $1,000 per "
        f"violation pursuant to 15 U.S.C. § 1681n."
    )
