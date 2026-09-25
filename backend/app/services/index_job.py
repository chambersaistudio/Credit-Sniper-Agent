"""
Stage 1 as a standalone, independently checkpointed job.

Deliberately NOT wired into the upload worker yet. The full-report extraction
it is meant to replace still fails on large reports, so running both would
just pay Sol to fail after the index succeeded. This runs the index pass on
its own, banks the result, and stops — which is what validating the index on a
real PDF requires.

The index lands in `extraction_checkpoint["index"]`, beside (not instead of)
the extraction checkpoint, so it survives independently: once batching exists,
a report that already has an index never pays to index it again.

Nothing here can invoke the extraction or audit tiers.
"""
import logging
from datetime import datetime, timezone
from typing import Any

from app.database import async_session_maker
from app.models.credit_report import CreditReport
from app.services.document_extraction.index_quality import IndexQuality, assess_index
from app.services.document_extraction.index_schema import ReportIndex
from app.services.document_extraction.page_bundle import page_count
from app.services.document_extraction.pipeline import run_indexer
from app.services.storage import get_storage

logger = logging.getLogger(__name__)


class IndexResult:
    """What one index attempt produced, for an operator to read."""

    def __init__(self, index: ReportIndex | None, model: str | None, failure,
                 quality: IndexQuality, banked: bool):
        self.index = index
        self.model = model
        self.failure = failure
        self.quality = quality
        self.banked = banked
        # True when this index came from the checkpoint rather than a model
        # call — i.e. this invocation spent nothing.
        self.reused = False

    @property
    def ok(self) -> bool:
        return self.index is not None and self.quality.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "ok": self.ok,
            "banked": self.banked,
            "reused": self.reused,
            "quality": self.quality.to_dict(),
            "failure": self.failure.to_dict() if self.failure else None,
            "tradelines": [t.model_dump() for t in (self.index.tradelines if self.index else [])],
            "bureau": self.index.bureau if self.index else None,
            "document_created_date": self.index.document_created_date if self.index else None,
            "report_date": self.index.report_date if self.index else None,
            "score": self.index.score if self.index else None,
            "score_type": self.index.score_type if self.index else None,
            "declared_count": self.index.tradeline_count if self.index else None,
            "total_pages": self.index.total_pages if self.index else None,
        }


def _banked_index(report: CreditReport):
    """A previously banked PASSING index for this report, or None.

    Only a passing one counts. A failed index is never banked in the first
    place, but an older row could carry one, and reusing it would propagate a
    bad page map into every batch."""
    checkpoint = report.extraction_checkpoint or {}
    stored = checkpoint.get("index")
    if not stored:
        return None
    try:
        index = ReportIndex.model_validate(stored)
    except Exception:  # a checkpoint from an older schema is not reusable
        logger.warning("Report %s: banked index does not match the current schema; re-indexing",
                       report.id)
        return None
    quality = assess_index(index)
    if not quality.ok:
        return None
    return index, checkpoint.get("index_model"), quality


async def index_document(
    document: bytes, *, filename: str = "credit-report.pdf",
    context: dict[str, Any] | None = None, expected_tradelines: int | None = None,
) -> IndexResult:
    """Run the index pass over PDF bytes. No database involvement."""
    index, model, failure = await run_indexer(document, filename=filename, context=context or {})
    # Counted locally with pypdf. The one fact in the gate that does not
    # depend on the model being honest about what it read.
    try:
        actual_pages = page_count(document)
    except Exception:  # an unreadable container is the extractor's problem
        logger.warning("Could not count the pages of the supplied document")
        actual_pages = None
    quality = assess_index(index, expected_tradelines=expected_tradelines,
                           actual_page_count=actual_pages)
    return IndexResult(index, model, failure, quality, banked=False)


async def index_report(
    report_id, *, session_factory=None, expected_tradelines: int | None = None,
    bank_failed: bool = False, force: bool = False,
) -> IndexResult:
    """Index the original PDF already stored for a report, and checkpoint it.

    Idempotent: a report that already has a passing banked index is returned
    as-is rather than re-indexed. Running this command twice by hand must not
    buy the same index twice — a checkpoint is a promise that the work behind
    it need not be repeated, and that promise has to be honoured by the code
    that would otherwise repeat it. `force` re-indexes deliberately.

    Only a passing index is banked: an index that failed its quality gate is
    not work anyone should build on."""
    factory = session_factory or async_session_maker
    async with factory() as db:
        report = await db.get(CreditReport, report_id)
        if report is None:
            raise LookupError(f"no report {report_id}")
        if not report.storage_key:
            raise LookupError(f"report {report_id} has no stored original")

        existing = _banked_index(report)
        if existing is not None and not force:
            index, model, quality = existing
            logger.info("Report %s: reusing banked index (%d tradelines, model %s)",
                        report_id, quality.listed, model)
            result = IndexResult(index, model, None, quality, banked=True)
            result.reused = True
            return result

        document = await get_storage().get(report.storage_key)
        result = await index_document(
            document,
            context={"user_id": str(report.user_id), "report_id": str(report.id), "stage": "index"},
            expected_tradelines=expected_tradelines,
        )

        if result.index is not None and (result.quality.ok or bank_failed):
            # Rebuilt rather than mutated: a plain JSON column compares the old
            # value to the new one, so mutating the dict already on the row
            # leaves the update invisible to the flush.
            checkpoint = dict(report.extraction_checkpoint or {})
            report.extraction_checkpoint = {
                **checkpoint,
                "index": result.index.model_dump(mode="json"),
                "index_model": result.model,
                "index_quality": result.quality.to_dict(),
                "indexed_at": datetime.now(timezone.utc).isoformat(),
            }
            await db.commit()
            result.banked = True
            logger.info("Report %s: index banked (%d tradelines, model %s)",
                        report_id, result.quality.listed, result.model)
        elif result.failure:
            logger.warning("Report %s: index pass failed (%s)%s",
                           report_id, result.failure.error_class, result.failure.cost_note)
        else:
            logger.warning("Report %s: index rejected by the quality gate: %s",
                           report_id, "; ".join(result.quality.reasons))
        return result
