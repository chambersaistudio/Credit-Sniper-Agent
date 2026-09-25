"""
Stage 2 as independently checkpointed units of work.

One batch = up to four indexed tradelines + a transient PDF of just their
pages. Each batch banks itself the moment it passes, at

    extraction_checkpoint["batches"][batch_id]

so a batch that fails costs only itself. That is the rule the whole redesign
exists to satisfy: a failure on batch 3 must never re-buy batches 0 through 2.

Deliberately runs ONE batch per call. Nothing here iterates the plan — the
operator selects a batch, and no code path can quietly spend four times what
was asked for.
"""
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.database import async_session_maker
from app.models.credit_report import CreditReport
from app.services.checkpoint_claim import (
    DEFAULT_LEASE_SECONDS, BatchAlreadyRunning, SlotAlreadyRunning, batch_slot,
    drop_claim, lock_report, release, take_claim,
)
from app.services.document_extraction.batch_quality import BatchQuality, assess_batch
from app.services.document_extraction.batch_schema import TradelineBatch
from app.services.document_extraction.batching import (
    DEFAULT_BATCH_SIZE, DEFAULT_CONTEXT_PAGES, BatchPlan, plan_batches,
)
from app.services.document_extraction.index_schema import ReportIndex
from app.services.document_extraction.page_bundle import (
    PageBundle, RemapReport, build_bundle, remap_tradelines,
)
from app.services.document_extraction.pipeline import run_batch_extractor
from app.services.storage import get_storage

logger = logging.getLogger(__name__)


class NoBankedIndex(LookupError):
    """Stage 2 cannot run without a Stage-1 index to tell it where to look."""


class BatchResult:
    def __init__(self, plan: BatchPlan, batch: TradelineBatch | None, model: str | None,
                 failure, quality: BatchQuality, bundle: PageBundle | None,
                 remap: RemapReport | None, banked: bool, reused: bool = False):
        self.plan = plan
        self.batch = batch
        self.model = model
        self.failure = failure
        self.quality = quality
        self.bundle = bundle
        self.remap = remap
        self.banked = banked
        self.reused = reused

    @property
    def ok(self) -> bool:
        return self.batch is not None and self.quality.ok

    @property
    def accounts(self) -> list:
        return list(self.batch.accounts) if self.batch else []

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "ok": self.ok,
            "banked": self.banked,
            "reused": self.reused,
            "model": self.model,
            "quality": self.quality.to_dict(),
            "bundle": self.bundle.to_dict() if self.bundle else None,
            "remap": self.remap.to_dict() if self.remap else None,
            "failure": self.failure.to_dict() if self.failure else None,
            "accounts": [a.model_dump(mode="json") for a in self.accounts],
        }


def plan_fingerprint(plan: BatchPlan) -> str:
    """What a banked batch is FOR, independent of its ordinal id.

    "b0" is a position in a plan, not an identity. Re-indexing a report can
    put different tradelines at that position, and serving the old detail
    under the new name would attach one account's balances to another. The
    fingerprint is the tradeline identities and the pages, so any such change
    invalidates reuse."""
    payload = json.dumps({"keys": plan.keys, "pages": list(plan.pages)},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _entry_matches(entry: dict, plan: BatchPlan) -> bool:
    """Is this banked entry for the batch we are being asked about?

    An entry from before fingerprints existed falls back to comparing the
    stored plan's tradeline keys, and is rejected if it has neither."""
    stored = entry.get("plan_fingerprint")
    if stored:
        return stored == plan_fingerprint(plan)
    keys = (entry.get("plan") or {}).get("keys")
    return keys is not None and [list(k) for k in keys] == plan.keys


def banked_index(report: CreditReport) -> ReportIndex:
    """The Stage-1 index this report already paid for."""
    checkpoint = report.extraction_checkpoint or {}
    if not checkpoint.get("index"):
        raise NoBankedIndex(
            "this report has no banked index; run scripts/index_pass.py first"
        )
    return ReportIndex.model_validate(checkpoint["index"])


def batch_plans(report: CreditReport, *, batch_size: int = DEFAULT_BATCH_SIZE,
                context_pages: int = DEFAULT_CONTEXT_PAGES) -> list[BatchPlan]:
    return plan_batches(banked_index(report), batch_size=batch_size,
                        context_pages=context_pages)


async def extract_batch(
    document: bytes, plan: BatchPlan, *, context: dict[str, Any] | None = None,
    model: str | None = None, detail: str | None = None,
) -> BatchResult:
    """Read one batch from a bundle of its pages. No database involvement.

    The bundle is built from the Stage-1 index's page numbers, handed to one
    model call, and dropped. Page references come back relative to the bundle
    and are translated to original pages here — the model is never asked to
    know where its pages sat in the real report.
    """
    bundle = build_bundle(document, plan.pages, padding=plan.padding)
    batch, used_model, failure = await run_batch_extractor(
        bundle.pdf, plan.manifest(), bundle.page_count,
        filename=f"pages-{'-'.join(str(p) for p in plan.pages)}.pdf",
        context={**(context or {}), "batch_id": plan.batch_id},
        model=model, detail=detail,
    )

    remap = None
    if batch is not None:
        # The batch object too, not only its accounts: unreadable_pages is
        # stored on a failed checkpoint and must not stay bundle-relative.
        remap = remap_tradelines(batch.accounts, bundle, batch)
    quality = assess_batch(batch, plan, bundle, remap)
    return BatchResult(plan, batch, used_model, failure, quality, bundle, remap, banked=False)


async def run_report_batch(
    report_id, batch_id: str, *, session_factory=None,
    batch_size: int = DEFAULT_BATCH_SIZE, context_pages: int = DEFAULT_CONTEXT_PAGES,
    force: bool = False, bank_failed: bool = False,
    model: str | None = None, detail: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> BatchResult:
    """Run ONE batch of a report and checkpoint it independently.

    Three separate transactions, on purpose:

    1. **claim** — lock the report row, confirm this batch is neither banked
       nor already running, record an in-flight marker, commit. A second
       worker arriving now sees the claim instead of an empty slot and
       refuses, rather than buying the same pages again.
    2. **the model call** — no lock held, because it takes a minute and other
       batches of the same report must be able to run alongside it.
    3. **bank** — lock the row again, re-read the checkpoint FRESH, merge this
       batch into it, commit. Merging into a snapshot taken before step 2
       would erase any batch that finished while this one was running.
    """
    factory = session_factory or async_session_maker

    # ── 1. Claim ────────────────────────────────────────────────────────
    async with factory() as db:
        report = await lock_report(db, report_id)
        if not report.storage_key:
            raise LookupError(f"report {report_id} has no stored original")

        plans = batch_plans(report, batch_size=batch_size, context_pages=context_pages)
        plan = next((p for p in plans if p.batch_id == batch_id), None)
        if plan is None:
            raise LookupError(
                f"no batch {batch_id!r}; this report has {[p.batch_id for p in plans]}"
            )

        checkpoint = dict(report.extraction_checkpoint or {})
        banked = dict(checkpoint.get("batches") or {})
        existing = banked.get(batch_id)

        if existing and not force and _entry_matches(existing, plan):
            logger.info("Report %s batch %s already banked; reusing", report_id, batch_id)
            batch = TradelineBatch.model_validate(existing["batch"])
            stored = existing.get("quality") or {}
            quality = BatchQuality(**{**stored, "ok": True}) if stored else BatchQuality(
                ok=True, asked=plan.size, returned=len(batch.accounts),
                matched=len(batch.accounts))
            return BatchResult(plan, batch, existing.get("model"), None, quality,
                               None, None, banked=True, reused=True)
        if existing and not force and not _entry_matches(existing, plan):
            # The index changed under this batch id. "b0" is an ordinal, not
            # an identity: reusing it would serve one set of tradelines under
            # another set's name.
            logger.warning("Report %s batch %s was banked for different tradelines; re-reading",
                           report_id, batch_id)

        try:
            report.extraction_checkpoint = take_claim(
                checkpoint, batch_slot(batch_id), fingerprint=plan_fingerprint(plan),
                lease_seconds=lease_seconds, force=force,
            )
        except SlotAlreadyRunning:
            raise BatchAlreadyRunning(
                f"batch {batch_id} of report {report_id} is already being read"
            ) from None
        await db.commit()

    # ── 2. The expensive part, holding no lock ──────────────────────────
    try:
        async with factory() as db:
            report = await db.get(CreditReport, report_id)
            document = await get_storage().get(report.storage_key)
        result = await extract_batch(document, plan, model=model, detail=detail, context={
            "user_id": str(report.user_id), "report_id": str(report.id), "stage": "batch",
        })
    except BaseException:
        await release(factory, report_id, batch_slot(batch_id))
        raise

    # ── 3. Bank, merging into a freshly read checkpoint ─────────────────
    async with factory() as db:
        report = await lock_report(db, report_id)
        checkpoint = drop_claim(dict(report.extraction_checkpoint or {}), batch_slot(batch_id))
        banked = dict(checkpoint.get("batches") or {})

        if result.batch is not None and (result.quality.ok or bank_failed):
            banked[batch_id] = {
                "batch": result.batch.model_dump(mode="json"),
                "model": result.model,
                "quality": result.quality.to_dict(),
                "plan": plan.to_dict(),
                # What this entry is FOR. Checked on reuse so a re-indexed
                # report cannot serve stale detail under the same ordinal id.
                "plan_fingerprint": plan_fingerprint(plan),
                "bundle": result.bundle.to_dict() if result.bundle else None,
                "remap": result.remap.to_dict() if result.remap else None,
                "extracted_at": datetime.now(timezone.utc).isoformat(),
            }
            result.banked = True
            logger.info("Report %s batch %s banked (%d tradelines, model %s)",
                        report_id, batch_id, len(result.accounts), result.model)
        elif result.failure:
            logger.warning("Report %s batch %s failed (%s)%s",
                           report_id, batch_id, result.failure.error_class,
                           result.failure.cost_note)
        else:
            logger.warning("Report %s batch %s rejected by the quality gate: %s",
                           report_id, batch_id, "; ".join(result.quality.reasons))

        # Rebuilt, never mutated: a plain JSON column compares old to new, so
        # mutating the dict already on the row hides the update from the flush.
        report.extraction_checkpoint = {**checkpoint, "batches": banked}
        await db.commit()
        return result


def batch_status(report: CreditReport, *, batch_size: int = DEFAULT_BATCH_SIZE,
                 context_pages: int = DEFAULT_CONTEXT_PAGES) -> list[dict[str, Any]]:
    """Which batches exist and which have been paid for."""
    banked = (report.extraction_checkpoint or {}).get("batches") or {}
    rows = []
    for plan in batch_plans(report, batch_size=batch_size, context_pages=context_pages):
        entry = banked.get(plan.batch_id)
        rows.append({
            **plan.to_dict(),
            "banked": bool(entry),
            "model": (entry or {}).get("model"),
            "extracted_at": (entry or {}).get("extracted_at"),
        })
    return rows
