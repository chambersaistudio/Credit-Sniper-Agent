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

AUTO_LINK_THRESHOLD = 0.7

# Weights sum to 1.0 and are NOT renormalized over whichever signals happen
# to be present: a missing account number must lower confidence, not let
# name similarity alone clear the threshold.
_WEIGHTS = {"account_number_suffix": 0.45, "creditor_name": 0.4, "date_opened": 0.15}

_CREDITOR_SUFFIXES = re.compile(
    r"\b(bank|na|llc|inc|corp|corporation|co|company|usa|bk|assn|association)\b", re.IGNORECASE
)
_NON_ALNUM = re.compile(r"[^a-z0-9 ]")


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


def score_match(a: dict[str, Any], b: dict[str, Any]) -> MatchResult:
    """Score two tradelines (dicts with creditor_name, account_number, date_opened)."""
    matched: dict[str, float] = {}

    suffix_a = account_number_suffix(a.get("account_number"))
    suffix_b = account_number_suffix(b.get("account_number"))
    if suffix_a and suffix_b:
        if suffix_a != suffix_b:
            return MatchResult(0.0, {"account_number_suffix": 0.0})
        matched["account_number_suffix"] = 1.0

    name_a = normalize_creditor_name(a.get("creditor_name"))
    name_b = normalize_creditor_name(b.get("creditor_name"))
    matched["creditor_name"] = round(SequenceMatcher(None, name_a, name_b).ratio(), 3) if name_a and name_b else 0.0

    opened_a = parse_report_date(a.get("date_opened"))
    opened_b = parse_report_date(b.get("date_opened"))
    if opened_a and opened_b:
        days_apart = abs((opened_a - opened_b).days)
        # Full credit within ~2 months; linear falloff to 0 by a year apart.
        matched["date_opened"] = round(max(0.0, 1.0 - max(0, days_apart - 60) / 305), 3)

    confidence = sum(matched.get(name, 0.0) * weight for name, weight in _WEIGHTS.items())
    return MatchResult(round(confidence, 3), matched)


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
    """Best candidate above threshold whose slot for this bureau isn't
    already taken by another record from the same report, scoring against
    every record it holds and keeping the strongest."""
    best: Candidate | None = None
    best_result = MatchResult(0.0)
    for candidate in candidates:
        if candidate.occupied_by(bureau, report_id):
            continue
        for other in candidate.records:
            result = score_match(record, other.fields)
            if result.confidence > best_result.confidence:
                best, best_result = candidate, result
    if best is None or best_result.confidence < AUTO_LINK_THRESHOLD:
        return None, best_result
    return best, best_result


def _match_fields(account: CreditAccount) -> dict[str, Any]:
    return {
        "creditor_name": account.creditor_name,
        "account_number": account.account_number,
        "date_opened": account.date_opened,
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

    for account in accounts:
        fields = _match_fields(account)
        candidate, match = choose_candidate(fields, account.bureau, account.report_id, candidates)
        if candidate is None:
            canonical = CanonicalAccount(
                user_id=user_id,
                creditor_name=account.creditor_name or "Unknown creditor",
                account_type=account.account_type,
                account_number_last_four=account_number_suffix(account.account_number),
            )
            db.add(canonical)
            await db.flush()
            candidate = Candidate(canonical_id=canonical.id, records=[])
            candidates.append(candidate)
            match = MatchResult(1.0, {"first_record": 1.0})

        candidate.records.append(LinkedRecord(account.bureau, account.report_id, fields))
        db.add(AccountLink(
            canonical_account_id=candidate.canonical_id,
            credit_account_id=account.id,
            bureau=account.bureau,
            confidence=match.confidence,
            matched_fields=match.matched_fields,
        ))
