"""
Two different things a credit report says about an account.

  account_lifecycle    is the account OPEN or CLOSED
  payment_performance  is it being PAID AS AGREED, LATE, CHARGED OFF, …

Conflating them produced a real false positive: a Capital One account that
both Equifax and TransUnion print as **Closed** was read as a contradiction,
because Equifax's "Pays account as agreed" normalized to `current` (open-ish)
while TransUnion's "Paid or paying as agreed" normalized to `paid`
(closed-ish). Same account, same standing, different wording.

So: lifecycle comes only from what the report says about open/closed — never
inferred from a payment phrase — and performance normalizes the wording
bureaus use for the same standing onto one value.
"""
import re
from typing import Any

OPEN = "open"
CLOSED = "closed"

# Payment standing, normalized. Bureau wording for the same thing varies
# wildly; these are the values we compare on.
AS_AGREED = "as_agreed"
LATE_30 = "late_30"
LATE_60 = "late_60"
LATE_90 = "late_90"
LATE_120 = "late_120"
CHARGED_OFF = "charged_off"
COLLECTION = "collection"
REPOSSESSION = "repossession"
FORECLOSURE = "foreclosure"
SETTLED = "settled"
IN_DISPUTE = "in_dispute"
DEFERRED = "deferred"

# Ordered: the first pattern that matches wins, so "120 days" beats "30".
_PERFORMANCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (CHARGED_OFF, re.compile(r"charge[ds]?[\s-]?off|charged off as bad debt|bad debt", re.I)),
    (COLLECTION, re.compile(r"collection|placed for collection|assigned to (?:an? )?(?:attorney|collection)", re.I)),
    (REPOSSESSION, re.compile(r"repossess|voluntary surrender|voluntarily surrendered", re.I)),
    (FORECLOSURE, re.compile(r"foreclos", re.I)),
    (SETTLED, re.compile(r"settled|paid (?:for )?less than (?:the )?full|legally paid in full for less", re.I)),
    (LATE_120, re.compile(r"\b(120|150|180)\b[\s+]*days?|120\+", re.I)),
    (LATE_90, re.compile(r"\b90\b[\s+]*days?", re.I)),
    (LATE_60, re.compile(r"\b60\b[\s+]*days?", re.I)),
    (LATE_30, re.compile(r"\b30\b[\s+]*days?|past due(?!\s*amount)", re.I)),
    (IN_DISPUTE, re.compile(r"dispute", re.I)),
    (DEFERRED, re.compile(r"deferred|forbearance|deferment", re.I)),
    # Everything a bureau says to mean "this account is in good standing".
    # Equifax's "Pays account as agreed" and TransUnion's "Paid or paying as
    # agreed" are the same statement and must normalize identically.
    (AS_AGREED, re.compile(
        r"pays?\s+(?:account\s+)?as\s+agreed"
        r"|paid\s+or\s+paying\s+as\s+agreed"
        r"|pays?\s+or\s+paid\s+as\s+agreed"
        r"|paying\s+as\s+agreed"
        r"|paid\s+as\s+agreed"
        r"|as\s+agreed"
        r"|never\s+late"
        r"|current(?:\s+account)?"
        r"|paid[,/\s]+closed"
        r"|paid\s+in\s+full"
        r"|account\s+(?:paid|closed)\s*[,/]?\s*(?:never\s+late)?$"
        r"|^paid$"
        r"|in\s+good\s+standing",
        re.I,
    )),
)

_CLOSED_WORDS = re.compile(r"\bclosed\b|\bterminated\b|\bpaid\s+and\s+closed\b", re.I)
_OPEN_WORDS = re.compile(r"\bopen\b|\bactive\b", re.I)


def account_lifecycle(record: dict[str, Any]) -> str | None:
    """OPEN / CLOSED / None, from what the report states about the account's
    lifecycle — never from a payment phrase.

    Priority: the report's own open/closed field, then a closing date, then an
    unambiguous open/closed word in the raw status. "Paid or paying as agreed"
    says nothing about lifecycle and is ignored here on purpose."""
    stored = record.get("account_lifecycle")
    if stored in (OPEN, CLOSED):
        return stored
    stated = (record.get("open_closed") or record.get("raw_data", {}).get("open_closed") or "").strip()
    if stated:
        if _CLOSED_WORDS.search(stated):
            return CLOSED
        if _OPEN_WORDS.search(stated):
            return OPEN
    if record.get("date_closed"):
        return CLOSED
    # The normalized status column, but ONLY when it holds a lifecycle word.
    # "current" and "paid" are payment standings and say nothing here — that
    # conflation is exactly what produced the Capital One false positive.
    normalized = (record.get("account_status") or "").strip().lower()
    if normalized in (OPEN, CLOSED):
        return normalized
    raw = (record.get("account_status_raw") or "")
    # Only when the word appears on its own — "Paid or paying as agreed"
    # must not be read as closed just because it contains "paid".
    closed, opened = _CLOSED_WORDS.search(raw), _OPEN_WORDS.search(raw)
    if closed and not opened:
        return CLOSED
    if opened and not closed:
        return OPEN
    return None


def payment_performance(record: dict[str, Any]) -> str | None:
    """How the account is being paid, normalized so that wording differences
    between bureaus don't read as a disagreement."""
    stored = record.get("payment_performance")
    if stored:
        return stored
    text = " ".join(
        str(value) for value in (
            record.get("account_status_raw"), record.get("payment_status"),
        ) if value
    ).strip()
    if not text:
        return None
    for performance, pattern in _PERFORMANCE_PATTERNS:
        if pattern.search(text):
            return performance
    return None
