"""
Storing and resolving benchmark truth.

Truth is entered or corrected once and thereafter referenced. That is partly
ergonomics — nobody should retype four accounts of payment grid on a phone —
and partly data handling: truth is real account data, so the fewer places it
is typed, pasted and copied, the fewer places it leaks from.

Two rules the resolver enforces rather than documents:

* **Unverified truth cannot be benchmarked against.** A draft prefilled from a
  model's own extraction is a convenience, not truth. Scoring against it would
  measure agreement with that model instead of correctness, which is worse than
  no measurement because it looks like one.
* **A job runs against the truth it was queued for.** The fingerprint travels
  with the job, so a truth corrected between queueing and running fails the
  job rather than quietly answering a different question.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.benchmark_truth import BenchmarkTruth

DEFAULT_LABEL = "current"

# Fields a truth entry may state. Anything else is rejected rather than stored
# and silently ignored — a misspelled key would otherwise look like a field the
# model failed, when in fact nothing was scoring it.
from app.services.benchmark.groundtruth import SCORED_FIELDS  # noqa: E402

ALLOWED_ACCOUNT_KEYS = frozenset(SCORED_FIELDS) | {
    "creditor_name", "account_number", "source_pages", "payment_history",
    "payment_history_complete", "note",
}


class TruthError(ValueError):
    """The supplied truth cannot be stored."""


class TruthUnverified(ValueError):
    """Truth exists but nobody has confirmed it against the document."""


class TruthChanged(ValueError):
    """The stored truth is not the one this job was queued against."""


def fingerprint(accounts: dict[str, Any]) -> str:
    """Stable hash of a truth set, so a correction is detectable."""
    return hashlib.sha256(
        json.dumps(accounts, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def validate(accounts: Any) -> dict[str, Any]:
    """Check the shape before storing it.

    Deliberately strict about unknown keys: a truth file with `ballance` would
    otherwise store fine and score nothing, and the benchmark would report a
    field as unmeasured rather than as a typo."""
    if not isinstance(accounts, dict):
        raise TruthError("truth must be an object with an 'accounts' array")
    rows = accounts.get("accounts")
    if not isinstance(rows, list) or not rows:
        raise TruthError("truth must contain at least one account")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise TruthError(f"account {index} is not an object")
        if not str(row.get("creditor_name") or "").strip():
            raise TruthError(f"account {index} has no creditor_name")
        unknown = sorted(set(row) - ALLOWED_ACCOUNT_KEYS)
        if unknown:
            raise TruthError(
                f"account {index} has unrecognised field(s) {unknown}; "
                f"nothing would score them"
            )
        history = row.get("payment_history")
        if history is not None:
            if not isinstance(history, dict):
                raise TruthError(f"account {index}: payment_history must be a YYYY-MM map")
            for month in history:
                if not (isinstance(month, str) and len(month) == 7 and month[4] == "-"
                        and month[:4].isdigit() and month[5:].isdigit()
                        and 1 <= int(month[5:]) <= 12):
                    raise TruthError(
                        f"account {index}: {month!r} is not a YYYY-MM month"
                    )
    return {"accounts": rows}


async def get(db: AsyncSession, report_id, batch_id: str,
              label: str = DEFAULT_LABEL) -> BenchmarkTruth | None:
    return (await db.execute(
        select(BenchmarkTruth).where(
            BenchmarkTruth.report_id == uuid.UUID(str(report_id)),
            BenchmarkTruth.batch_id == batch_id,
            BenchmarkTruth.label == label,
        )
    )).scalar_one_or_none()


async def list_for_report(db: AsyncSession, report_id) -> list[BenchmarkTruth]:
    return list((await db.execute(
        select(BenchmarkTruth)
        .where(BenchmarkTruth.report_id == uuid.UUID(str(report_id)))
        .order_by(BenchmarkTruth.batch_id, BenchmarkTruth.label)
    )).scalars().all())


async def upsert(db: AsyncSession, *, report_id, batch_id: str, accounts: dict[str, Any],
                 created_by: str, label: str = DEFAULT_LABEL, verified: bool = False,
                 source: str = "operator", note: str | None = None) -> BenchmarkTruth:
    """Store or correct a truth set.

    Correcting it resets `verified` unless the caller says otherwise, because
    the confirmation was of the previous values."""
    payload = validate(accounts)
    existing = await get(db, report_id, batch_id, label)
    if existing is None:
        existing = BenchmarkTruth(
            id=uuid.uuid4(), report_id=uuid.UUID(str(report_id)), batch_id=batch_id,
            label=label, created_by=created_by,
        )
        db.add(existing)
    existing.accounts = payload
    existing.fingerprint = fingerprint(payload)
    existing.verified = verified
    existing.source = source
    existing.note = note
    existing.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return existing


async def set_verified(db: AsyncSession, truth: BenchmarkTruth, verified: bool) -> BenchmarkTruth:
    truth.verified = verified
    truth.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return truth


async def resolve_for_job(db: AsyncSession, report_id, batch_id: str, label: str,
                          expected_fingerprint: str | None = None) -> dict[str, Any]:
    """The accounts a benchmark should score against, or an explicit failure.

    Called at queue time (to record the fingerprint) and again at run time (to
    prove the truth has not moved since)."""
    truth = await get(db, report_id, batch_id, label)
    if truth is None:
        raise TruthError(
            f"no benchmark truth stored for batch {batch_id} (label {label!r}); "
            f"save it first"
        )
    if not truth.verified:
        raise TruthUnverified(
            f"benchmark truth for batch {batch_id} is not verified; confirm it against "
            f"the document before scoring a model against it"
        )
    if expected_fingerprint is not None and truth.fingerprint != expected_fingerprint:
        raise TruthChanged(
            "the stored benchmark truth changed after this job was queued; "
            "queue a new job so the result matches the truth it is scored against"
        )
    return truth.accounts


def draft_from_batch(banked: dict[str, Any]) -> dict[str, Any]:
    """A truth DRAFT prefilled from a banked extraction.

    Purely an ergonomic head start: it gives an operator the accounts and
    fields to correct instead of typing them out on a phone. It is not truth
    and is stored unverified, because scoring a model against its own output
    measures self-consistency and nothing else.
    """
    accounts = ((banked or {}).get("batch") or {}).get("accounts") or []
    rows = []
    for account in accounts:
        row: dict[str, Any] = {"creditor_name": account.get("creditor_name")}
        for field in ("original_creditor", "account_number", "account_type", "open_closed",
                      "status_raw", "payment_status", "balance", "credit_limit",
                      "past_due_amount", "original_amount", "date_opened", "date_closed",
                      "balance_updated", "date_last_reported", "remarks"):
            if account.get(field) is not None:
                row[field] = account[field]
        if account.get("source_pages"):
            row["source_pages"] = account["source_pages"]
        history = {
            f"{entry['year']}-{int(entry['month']):02d}": entry.get("raw_status_code")
            for entry in (account.get("payment_history") or [])
            if entry.get("year") and entry.get("month")
        }
        if history:
            row["payment_history"] = history
        rows.append(row)
    if not rows:
        raise TruthError("that banked batch holds no accounts to draft from")
    return {"accounts": rows}


def to_dict(truth: BenchmarkTruth) -> dict[str, Any]:
    return {
        "report_id": str(truth.report_id),
        "batch_id": truth.batch_id,
        "label": truth.label,
        "accounts": (truth.accounts or {}).get("accounts") or [],
        "account_count": truth.account_count,
        "fingerprint": truth.fingerprint,
        "verified": truth.verified,
        "source": truth.source,
        "note": truth.note,
        "created_by": truth.created_by,
        "created_at": truth.created_at.isoformat() if truth.created_at else None,
        "updated_at": truth.updated_at.isoformat() if truth.updated_at else None,
    }


def summary(truth: BenchmarkTruth) -> dict[str, Any]:
    """The listing view: whether truth exists and is usable, without its
    contents — so a report overview carries no account values at all."""
    return {
        "batch_id": truth.batch_id,
        "label": truth.label,
        "account_count": truth.account_count,
        "months": sum(len(row.get("payment_history") or {})
                      for row in ((truth.accounts or {}).get("accounts") or [])),
        "verified": truth.verified,
        "source": truth.source,
        "fingerprint": truth.fingerprint,
        "updated_at": truth.updated_at.isoformat() if truth.updated_at else None,
    }
