"""
Cross-bureau account matching.

Decides whether bureau-specific tradelines (one CreditAccount row each) are
the same real-world account, and links them to a shared CanonicalAccount.
Deliberately conservative — "do not assume slightly different accounts are
identical solely because names are similar": a record either clears the
confidence threshold or becomes its own canonical account. Never forced.

Hard constraint: a canonical account holds at most one record per bureau
*per report*. Two Experian tradelines from the same report are never the
same account, however alike they look — that's a duplicate-reporting
question for the reasoning engine, not a merge. A record from a newer
Experian report can join the canonical account holding last month's
Experian record: that's the same tradeline, re-reported.
"""
import re
import uuid
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.canonical_account import AccountLink, CanonicalAccount
from app.models.credit_report import CreditAccount
from app.utils.dates import parse_report_date

AUTO_LINK_THRESHOLD = 0.72
# Between the two: plausibly the same account, but not provably. The record
# gets its own canonical account AND a recorded review suggestion. Ambiguity
# is surfaced, never silently merged.
REVIEW_THRESHOLD = 0.45
# How much signal weight must be comparable before a score can reach its face
# value. Below this the confidence is scaled down: agreeing on a name alone is
# not the same as agreeing on a name, a number, a type and a date.
FULL_COVERAGE = 0.6

# Multi-signal, because masked account numbers are not trustworthy on their
# own — TransUnion's disclosure warns they may be scrambled. Weights sum to
# 1.0 over every signal; only the comparable ones count toward coverage.
_WEIGHTS = {
    "creditor_name": 0.26,
    "account_number_suffix": 0.24,
    "date_opened": 0.14,
    "original_creditor": 0.08,
    "account_type": 0.08,
    "amount_original_or_high": 0.06,
    "date_closed": 0.05,
    "payment_fingerprint": 0.05,
    "credit_limit": 0.04,
}

_CREDITOR_SUFFIXES = re.compile(
    r"\b(bank|na|llc|inc|corp|corporation|co|company|usa|bk|assn|association)\b", re.IGNORECASE
)
_NON_ALNUM = re.compile(r"[^a-z0-9 ]")
# A collection/debt-buyer tradeline and the original creditor's own tradeline
# are different items on the file, even when they concern the same debt.
_COLLECTION_TYPE = re.compile(r"collection|debt\s*(buyer|purchaser)|factoring\s*company", re.IGNORECASE)
# Below this, two original creditors are plainly different debts.
_ORIGINAL_CREDITOR_CONFLICT = 0.5


def normalize_creditor_name(name: str | None) -> str:
    if not name:
        return ""
    # Drop periods first so "N.A." collapses to the plain "na" suffix.
    normalized = name.lower().replace(".", "")
    normalized = _CREDITOR_SUFFIXES.sub(" ", normalized)
    normalized = _NON_ALNUM.sub(" ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def account_number_suffix(account_number: str | None) -> str | None:
    if not account_number:
        return None
    digits = re.sub(r"\D", "", account_number)
    return digits[-4:] if len(digits) >= 4 else (digits or None)


@dataclass
class MatchResult:
    confidence: float
    matched_fields: dict[str, float] = field(default_factory=dict)
    blocked_by: str | None = None

    @property
    def auto(self) -> bool:
        return self.confidence >= AUTO_LINK_THRESHOLD

    @property
    def needs_review(self) -> bool:
        return REVIEW_THRESHOLD <= self.confidence < AUTO_LINK_THRESHOLD


def is_collection(record: dict[str, Any]) -> bool:
    return bool(
        _COLLECTION_TYPE.search(record.get("account_type") or "")
        or record.get("account_status") == "collection"
    )


def _similarity(a: str | None, b: str | None) -> float | None:
    left, right = normalize_creditor_name(a), normalize_creditor_name(b)
    if not left or not right:
        return None
    return round(SequenceMatcher(None, left, right).ratio(), 3)


def _date_closeness(a: str | None, b: str | None, grace_days: int = 60, span_days: int = 305) -> float | None:
    left, right = parse_report_date(a), parse_report_date(b)
    if not left or not right:
        return None
    days_apart = abs((left - right).days)
    # Full credit within the grace period, linear falloff to 0 after that.
    return round(max(0.0, 1.0 - max(0, days_apart - grace_days) / span_days), 3)


def _amount_closeness(a: Any, b: Any, tolerance: float = 0.1) -> float | None:
    try:
        left, right = float(a), float(b)
    except (TypeError, ValueError):
        return None
    if left <= 0 and right <= 0:
        return None
    larger = max(abs(left), abs(right))
    if larger == 0:
        return None
    difference = abs(left - right) / larger
    return round(max(0.0, 1.0 - difference / tolerance), 3) if difference < tolerance else 0.0


def _fingerprint(history: Any) -> dict[tuple[int, int], str]:
    """(year, month) -> status, for comparing two grids month by month."""
    cells: dict[tuple[int, int], str] = {}
    for entry in history or []:
        try:
            key = (int(entry["year"]), int(entry["month"]))
        except (KeyError, TypeError, ValueError):
            continue
        code = entry.get("raw_status_code") or entry.get("status_code") or entry.get("raw_code") or entry.get("code")
        if code:
            cells[key] = str(code).strip().upper()
    return cells


def _payment_fingerprint(a: Any, b: Any, minimum_overlap: int = 3) -> float | None:
    left, right = _fingerprint(a), _fingerprint(b)
    shared = set(left) & set(right)
    if len(shared) < minimum_overlap:
        return None
    agreeing = sum(left[key] == right[key] for key in shared)
    return round(agreeing / len(shared), 3)


def score_match(a: dict[str, Any], b: dict[str, Any]) -> MatchResult:
    """Score two bureau tradelines as the same real-world account.

    Deliberately multi-signal. A masked account number can be scrambled, so it
    never decides a match by itself — but neither does a familiar creditor
    name. Signals that can't be compared don't count toward coverage, and low
    coverage scales the result down, so thin evidence lands in review rather
    than auto-merging."""
    # Hard blocks. A collection and the original creditor's own tradeline are
    # related but distinct entries — merging them would erase the relationship
    # the consumer needs to see (and dispute) separately.
    if is_collection(a) != is_collection(b):
        return MatchResult(0.0, {}, blocked_by="collection_vs_original_tradeline")
    originals = _similarity(a.get("original_creditor"), b.get("original_creditor"))
    if originals is not None and originals < _ORIGINAL_CREDITOR_CONFLICT:
        # Same debt buyer, different underlying debts.
        return MatchResult(0.0, {"original_creditor": originals}, blocked_by="different_original_creditor")

    signals: dict[str, float] = {}

    suffix_a = account_number_suffix(a.get("account_number"))
    suffix_b = account_number_suffix(b.get("account_number"))
    if suffix_a and suffix_b:
        # A mismatch is evidence against, not a veto: bureaus sometimes
        # scramble the digits they show.
        signals["account_number_suffix"] = 1.0 if suffix_a == suffix_b else 0.0

    for name, value in (
        ("creditor_name", _similarity(a.get("creditor_name"), b.get("creditor_name"))),
        ("original_creditor", originals),
        ("account_type", _similarity(a.get("account_type"), b.get("account_type"))),
        ("date_opened", _date_closeness(a.get("date_opened"), b.get("date_opened"))),
        ("date_closed", _date_closeness(a.get("date_closed"), b.get("date_closed"))),
        ("credit_limit", _amount_closeness(a.get("credit_limit"), b.get("credit_limit"))),
        ("amount_original_or_high", _amount_closeness(
            a.get("original_amount") or a.get("high_balance"), b.get("original_amount") or b.get("high_balance"))),
        ("payment_fingerprint", _payment_fingerprint(a.get("payment_history"), b.get("payment_history"))),
    ):
        if value is not None:
            signals[name] = value

    available = sum(_WEIGHTS[name] for name in signals)
    if not available:
        return MatchResult(0.0, {})
    agreement = sum(signals[name] * _WEIGHTS[name] for name in signals) / available
    coverage = min(1.0, available / FULL_COVERAGE)
    confidence = agreement * coverage

    # Two numbers that both exist and disagree is a real conflict. The digits
    # may well have been scrambled — which is why it isn't a veto — but it is
    # never resolved silently: the best such a pair can reach is review.
    if signals.get("account_number_suffix") == 0.0:
        confidence = min(confidence, AUTO_LINK_THRESHOLD - 0.01)

    return MatchResult(round(confidence, 3), signals)


@dataclass
class LinkedRecord:
    bureau: str
    report_id: uuid.UUID
    fields: dict[str, Any]


@dataclass
class Candidate:
    """A canonical account as the matcher sees it: every record linked to it."""

    canonical_id: uuid.UUID
    records: list[LinkedRecord]

    def occupied_by(self, bureau: str, report_id: uuid.UUID) -> bool:
        return any(r.bureau == bureau and r.report_id == report_id for r in self.records)


def choose_candidate(
    record: dict[str, Any], bureau: str, report_id: uuid.UUID, candidates: list[Candidate]
) -> tuple[Candidate | None, MatchResult]:
    """Best candidate whose slot for this bureau isn't already taken by another
    record from the same report, scoring against every record it holds.

    Returns the candidate only when the score clears the auto-link threshold.
    A near miss still comes back in the MatchResult so the caller can record a
    review suggestion instead of merging."""
    best: Candidate | None = None
    best_result = MatchResult(0.0)
    for candidate in candidates:
        if candidate.occupied_by(bureau, report_id):
            continue
        for other in candidate.records:
            result = score_match(record, other.fields)
            if result.confidence > best_result.confidence:
                best, best_result = candidate, result
    if best is None or not best_result.auto:
        return None, best_result
    return best, best_result


def _match_fields(account: CreditAccount) -> dict[str, Any]:
    return {
        "creditor_name": account.creditor_name,
        "original_creditor": account.original_creditor,
        "sold_to": account.sold_to,
        "account_number": account.account_number,
        "account_type": account.account_type,
        "account_status": account.account_status,
        "date_opened": account.date_opened,
        "date_closed": account.date_closed,
        "credit_limit": account.credit_limit,
        "original_amount": account.original_amount,
        "high_balance": account.high_balance,
        "payment_history": account.payment_history,
    }


async def link_accounts(db: AsyncSession, user_id: uuid.UUID, accounts: list[CreditAccount]) -> None:
    """Link each newly stored CreditAccount (already flushed, so it has an id)
    to an existing or new CanonicalAccount. Loads the user's candidates once
    and updates them in memory, so records within one upload are matched
    against each other's assignments too."""
    result = await db.execute(
        select(CanonicalAccount)
        .where(CanonicalAccount.user_id == user_id)
        .options(selectinload(CanonicalAccount.links).selectinload(AccountLink.credit_account))
    )
    candidates = [
        Candidate(
            canonical_id=c.id,
            records=[
                LinkedRecord(link.bureau, link.credit_account.report_id, _match_fields(link.credit_account))
                for link in c.links
            ],
        )
        for c in result.scalars().all()
    ]

    by_id = {c.canonical_id: c for c in candidates}

    for account in accounts:
        fields = _match_fields(account)
        candidate, match = choose_candidate(fields, account.bureau, account.report_id, candidates)
        if candidate is None:
            # Not confident enough to merge. The record becomes its own
            # account; if it was a near miss, that is recorded so a human can
            # confirm or reject the merge later.
            review = None
            if match.needs_review:
                near = next(
                    (c for c in candidates
                     if not c.occupied_by(account.bureau, account.report_id)
                     and any(score_match(fields, o.fields).confidence == match.confidence for o in c.records)),
                    None,
                )
                if near is not None:
                    review = {
                        "candidate_canonical_id": str(near.canonical_id),
                        "confidence": match.confidence,
                        "matched_fields": match.matched_fields,
                        "reason": "Looks like the same account, but not confidently enough to merge.",
                    }
            canonical = CanonicalAccount(
                user_id=user_id,
                creditor_name=account.creditor_name or "Unknown creditor",
                account_type=account.account_type,
                account_number_last_four=account_number_suffix(account.account_number),
                match_review=review,
            )
            db.add(canonical)
            await db.flush()
            candidate = Candidate(canonical_id=canonical.id, records=[])
            candidates.append(candidate)
            by_id[canonical.id] = candidate
            match = MatchResult(1.0, {"first_record": 1.0})

        candidate.records.append(LinkedRecord(account.bureau, account.report_id, fields))
        db.add(AccountLink(
            canonical_account_id=candidate.canonical_id,
            credit_account_id=account.id,
            bureau=account.bureau,
            confidence=match.confidence,
            matched_fields=match.matched_fields,
        ))
