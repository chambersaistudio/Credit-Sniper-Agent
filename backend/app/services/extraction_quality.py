"""
Extraction quality gate.

A deterministic parse (or an AI extraction) is NOT successful merely because
it returned one or more account rows. A parse that yields mostly empty rows,
rows that are just "original creditor" names, or rows that don't resemble
tradelines is a *failed* extraction — it must trigger the verified AI
fallback, and it must block dispute evaluation rather than letting the
reasoning engine conclude "no dispute ground" from missing evidence.

Everything here is deterministic and reads only structured fields, so it runs
before any AI call and again on the AI result.
"""
from dataclasses import dataclass, field
from typing import Any

# Fields that make a row look like a real tradeline rather than a bare name.
_CORE_FIELDS = (
    "account_number", "account_type", "account_status", "payment_status",
    "balance", "past_due_amount", "high_balance", "credit_limit", "original_amount",
    "date_opened", "date_closed", "date_last_reported", "date_last_payment", "date_of_first_delinquency",
)


def _present(value: Any) -> bool:
    return value not in (None, "", [], {})


def _core_count(account: dict[str, Any]) -> int:
    return sum(1 for f in _CORE_FIELDS if _present(account.get(f)))


def account_is_substantial(account: dict[str, Any]) -> bool:
    """A real tradeline: it has a name and at least two reportable fields."""
    return _present(account.get("creditor_name")) and _core_count(account) >= 2


def record_is_disputable(record: dict[str, Any]) -> bool:
    """Enough was extracted for this one account to be evaluated for a
    dispute: a name plus at least one reportable field. A row that is only a
    name (a parse failure) is not."""
    return _present(record.get("creditor_name")) and _core_count(record) >= 1


def _only_original_creditor(account: dict[str, Any]) -> bool:
    return (
        not _present(account.get("creditor_name"))
        and _present(account.get("original_creditor"))
    ) or _core_count(account) == 0


@dataclass
class ExtractionQuality:
    complete: bool
    total: int
    substantial: int
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"complete": self.complete, "total": self.total,
                "substantial": self.substantial, "reasons": self.reasons}


def assess_accounts(accounts: list[dict[str, Any]]) -> ExtractionQuality:
    total = len(accounts)
    if total == 0:
        return ExtractionQuality(False, 0, 0, ["No accounts were extracted."])

    substantial = sum(account_is_substantial(a) for a in accounts)
    with_number = sum(_present(a.get("account_number")) for a in accounts)
    with_core = sum(_core_count(a) >= 2 for a in accounts)
    only_original = sum(_only_original_creditor(a) for a in accounts)

    reasons: list[str] = []
    if substantial * 2 < total:
        reasons.append(f"Only {substantial} of {total} extracted accounts look like complete tradelines.")
    if with_number * 2 < total:
        reasons.append(f"{total - with_number} of {total} accounts have no account number.")
    if with_core * 2 < total:
        reasons.append(f"{total - with_core} of {total} accounts are missing type/status/balance/date fields.")
    if only_original * 2 >= total:
        reasons.append("Most extracted rows are just original-creditor names, not tradelines.")

    return ExtractionQuality(complete=not reasons, total=total, substantial=substantial, reasons=reasons)


def inquiry_is_suspicious(inquiry: dict[str, Any]) -> bool:
    name = (inquiry.get("creditor_name") or "").strip()
    if len(name) < 2 or "prepared for" in name.lower():
        return True
    if not any(c.isalpha() for c in name):
        return True
    # A phone number or address fragment, not a furnisher.
    digits = sum(c.isdigit() for c in name)
    return digits >= 5 or name.startswith("(")
