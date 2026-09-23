"""
Cross-bureau account matching.

Decides whether two bureau-specific tradelines (one CreditAccount row each,
from different bureau reports) are the same real-world account. This is
deliberately conservative: "do not assume slightly different accounts are
identical solely because names are similar" (Phase 1.5 spec). A pair either
clears the confidence threshold and gets linked to a shared
CanonicalAccount, or it doesn't and stays its own single-record canonical
account — there's no forced/best-guess match.

Pure scoring logic (`score_match`) is separated from DB I/O
(`match_account_to_existing`) so the matching rules can be unit tested
without a database.
"""
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical_account import CanonicalAccount, AccountLink
from app.models.credit_report import CreditAccount

# Below this, a pair is not linked — left as separate canonical accounts.
AUTO_LINK_THRESHOLD = 0.7

_CREDITOR_SUFFIXES = re.compile(
    r"\b(bank|na|llc|inc|corp|corporation|co|company|usa|bk|assn|association)\b",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9 ]")


def normalize_creditor_name(name: str | None) -> str:
    if not name:
        return ""
    normalized = name.lower()
    # Drop periods first so "N.A." collapses to "na" and matches the plain
    # "na" suffix below — \b won't reliably match right after a trailing
    # "." at end-of-string, since neither side of that boundary is \w.
    normalized = normalized.replace(".", "")
    normalized = _CREDITOR_SUFFIXES.sub(" ", normalized)
    normalized = _NON_ALNUM.sub(" ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def account_number_suffix(account_number: str | None) -> str | None:
    if not account_number:
        return None
    digits = re.sub(r"\D", "", account_number)
    return digits[-4:] if len(digits) >= 4 else (digits or None)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%m/%d/%Y", "%m/%Y", "%Y-%m-%d", "%Y-%m", "%m-%Y"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


@dataclass
class MatchResult:
    confidence: float
    matched_fields: dict[str, float] = field(default_factory=dict)


def score_match(a: dict[str, Any], b: dict[str, Any]) -> MatchResult:
    """
    Score whether two bureau tradelines (as dicts with creditor_name,
    account_number, date_opened) represent the same real-world account.
    """
    matched_fields: dict[str, float] = {}

    suffix_a = account_number_suffix(a.get("account_number"))
    suffix_b = account_number_suffix(b.get("account_number"))
    if suffix_a and suffix_b:
        if suffix_a != suffix_b:
            # Strong disqualifying signal — different account numbers on
            # both sides means different accounts, regardless of name.
            return MatchResult(confidence=0.0, matched_fields={"account_number_suffix": 0.0})
        matched_fields["account_number_suffix"] = 1.0

    name_a = normalize_creditor_name(a.get("creditor_name"))
    name_b = normalize_creditor_name(b.get("creditor_name"))
    name_similarity = SequenceMatcher(None, name_a, name_b).ratio() if name_a and name_b else 0.0
    matched_fields["creditor_name"] = round(name_similarity, 3)

    date_a = _parse_date(a.get("date_opened"))
    date_b = _parse_date(b.get("date_opened"))
    if date_a and date_b:
        days_apart = abs((date_a - date_b).days)
        # Full credit inside ~2 months (bureaus often report slightly
        # different open dates for the same account); linear falloff to 0
        # by a year apart.
        date_score = max(0.0, 1.0 - max(0, days_apart - 60) / 305)
        matched_fields["date_opened"] = round(date_score, 3)

    weights = {"account_number_suffix": 0.45, "creditor_name": 0.4, "date_opened": 0.15}
    # Weights sum to 1.0 by construction, and we deliberately do NOT
    # renormalize over only the signals that happen to be present: a
    # missing account number shouldn't let name similarity alone punch
    # above the auto-link threshold ("do not assume slightly different
    # accounts are identical solely because names are similar").
    confidence = sum(matched_fields.get(field, 0.0) * weight for field, weight in weights.items())
    return MatchResult(confidence=round(confidence, 3), matched_fields=matched_fields)


async def match_account_to_existing(
    db: AsyncSession, user_id: uuid.UUID, account: CreditAccount
) -> CanonicalAccount:
    """
    Find or create the CanonicalAccount for a newly-parsed CreditAccount.
    Compares against the user's other bureaus' accounts; links to the
    best match above AUTO_LINK_THRESHOLD, or creates a new canonical
    account (unlinked to anything yet) if nothing matches well enough.
    """
    candidate_result = await db.execute(
        select(CreditAccount, CanonicalAccount, AccountLink)
        .join(AccountLink, AccountLink.credit_account_id == CreditAccount.id)
        .join(CanonicalAccount, CanonicalAccount.id == AccountLink.canonical_account_id)
        .where(CanonicalAccount.user_id == user_id, CreditAccount.bureau != account.bureau)
    )

    account_dict = {
        "creditor_name": account.creditor_name,
        "account_number": account.account_number,
        "date_opened": account.date_opened,
    }

    best_canonical: CanonicalAccount | None = None
    best_result = MatchResult(confidence=0.0)
    seen_canonical_ids: set[uuid.UUID] = set()
    for other_account, canonical, _link in candidate_result.all():
        if canonical.id in seen_canonical_ids:
            continue
        seen_canonical_ids.add(canonical.id)
        other_dict = {
            "creditor_name": other_account.creditor_name,
            "account_number": other_account.account_number,
            "date_opened": other_account.date_opened,
        }
        result = score_match(account_dict, other_dict)
        if result.confidence > best_result.confidence:
            best_result = result
            best_canonical = canonical

    if best_canonical is not None and best_result.confidence >= AUTO_LINK_THRESHOLD:
        canonical = best_canonical
    else:
        canonical = CanonicalAccount(
            user_id=user_id,
            creditor_name=account.creditor_name or "Unknown Creditor",
            account_type=account.account_type,
            account_number_last_four=account_number_suffix(account.account_number),
        )
        db.add(canonical)
        await db.flush()
        best_result = MatchResult(confidence=1.0, matched_fields={"first_record": 1.0})

    db.add(
        AccountLink(
            canonical_account_id=canonical.id,
            credit_account_id=account.id,
            bureau=account.bureau,
            confidence=best_result.confidence,
            matched_fields=best_result.matched_fields,
        )
    )
    return canonical
