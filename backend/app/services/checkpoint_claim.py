"""
Claiming a slot on a report before spending money on it.

Every paid pass — the Stage-1 index, each Stage-2 batch — has the same shape
and the same hazard: deciding "this hasn't been bought yet" and recording
"this is bought now" are separated by a model call that takes a minute. Two
workers arriving in that gap both see an empty slot and both pay.

The pattern, used identically by both:

    1. lock the report row, confirm the slot is neither done nor claimed,
       write a claim marker, commit            <- a rival now sees the claim
    2. release the lock and make the model call  <- minutes, no lock held
    3. re-lock, re-read the checkpoint FRESH, merge the result in, drop the
       claim, commit                            <- never overwrites a sibling

Step 3 re-reading is not incidental. Merging into a checkpoint snapshot taken
before step 2 would erase whatever another slot banked while this one ran,
which is how a paid batch disappears.

This lives in one module because two implementations of a money-guard drift,
and the one that drifts is the one nobody is looking at.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.credit_report import CreditReport

logger = logging.getLogger(__name__)

# Claims live together under one key so a report's in-flight work is visible
# in one place rather than scattered per pass.
CLAIMS = "claims"
DEFAULT_LEASE_SECONDS = 900

_LOCK = text("SELECT 1 FROM credit_reports WHERE id = :rid FOR UPDATE")


class SlotAlreadyRunning(RuntimeError):
    """Another worker holds the claim on this slot."""


class BatchAlreadyRunning(SlotAlreadyRunning):
    """Another worker is already reading this batch."""


class IndexAlreadyRunning(SlotAlreadyRunning):
    """Another worker is already indexing this report."""


def batch_slot(batch_id: str) -> str:
    return f"batch:{batch_id}"


INDEX_SLOT = "index"


async def lock_report(db: AsyncSession, report_id) -> CreditReport:
    """Load a report and hold its row for the rest of this transaction.

    The refresh after the lock matters: without it the session would serve the
    copy it already had, which is exactly the stale read the lock is meant to
    prevent."""
    report = await db.get(CreditReport, report_id)
    if report is None:
        raise LookupError(f"no report {report_id}")
    await db.execute(_LOCK, {"rid": str(report.id)})
    await db.refresh(report)
    return report


def claim_is_live(entry: dict | None, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> bool:
    """Is another worker still plausibly running this slot?

    A claim older than the lease is treated as abandoned: a worker that died
    must not strand the work forever. An unparseable timestamp counts as
    abandoned too — a marker nobody can read is not protecting anything."""
    if not entry:
        return False
    claimed = entry.get("claimed_at")
    if not claimed:
        return False
    try:
        started = datetime.fromisoformat(claimed)
    except (TypeError, ValueError):
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - started < timedelta(seconds=lease_seconds)


def take_claim(checkpoint: dict, slot: str, *, fingerprint: str | None = None,
               lease_seconds: int = DEFAULT_LEASE_SECONDS,
               force: bool = False) -> dict:
    """The checkpoint with a claim on `slot`, or raise if one is live.

    Returns a NEW dict. A plain JSON column compares the old value to the new
    one, so mutating the dict already on the row hides the update from the
    flush — and a claim nobody can see protects nothing."""
    claims = dict(checkpoint.get(CLAIMS) or {})
    if claim_is_live(claims.get(slot), lease_seconds) and not force:
        raise SlotAlreadyRunning(f"{slot} is already being worked on")
    claims[slot] = {
        "claimed_at": datetime.now(timezone.utc).isoformat(),
        **({"fingerprint": fingerprint} if fingerprint else {}),
    }
    return {**checkpoint, CLAIMS: claims}


def drop_claim(checkpoint: dict, slot: str) -> dict:
    """The checkpoint without this claim.

    The claims key is removed entirely once the last one goes, so a finished
    report's checkpoint holds only what it actually banked rather than an
    empty bookkeeping dict."""
    claims = dict(checkpoint.get(CLAIMS) or {})
    claims.pop(slot, None)
    without = {key: value for key, value in checkpoint.items() if key != CLAIMS}
    return {**without, CLAIMS: claims} if claims else without


async def release(factory, report_id, slot: str) -> None:
    """Drop a claim after a failure, so a retry is not blocked until the lease
    expires. Never raises: losing the release is recoverable (the lease still
    frees the slot), but masking the original failure would not be."""
    try:
        async with factory() as db:
            report = await lock_report(db, report_id)
            checkpoint = dict(report.extraction_checkpoint or {})
            if (checkpoint.get(CLAIMS) or {}).get(slot) is None:
                return
            report.extraction_checkpoint = drop_claim(checkpoint, slot)
            await db.commit()
    except Exception:
        logger.exception("Could not release the claim on report %s slot %s", report_id, slot)


def in_flight(report: CreditReport, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> list[str]:
    """Slots of this report currently claimed. For operator visibility."""
    claims = (report.extraction_checkpoint or {}).get(CLAIMS) or {}
    return sorted(slot for slot, entry in claims.items() if claim_is_live(entry, lease_seconds))


def claim_state(report: CreditReport) -> dict[str, Any]:
    return dict((report.extraction_checkpoint or {}).get(CLAIMS) or {})
