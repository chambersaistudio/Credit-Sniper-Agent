"""
Turning a banked Stage-1 index into independently payable units of work.

A batch is up to four indexed tradelines plus the original pages they live on.
Four is the size that keeps the detailed output — ~2,100 tokens per tradeline —
at roughly a quarter of the budget, leaving ample room for reasoning, which is
what the single-pass extraction ran out of.

Batches are derived from the index, never from the document's text. The plan
is deterministic: the same banked index always yields the same batches, with
the same ids, in the same order, so a batch checkpointed on one run refers to
exactly the same tradelines on the next.
"""
from dataclasses import dataclass
from typing import Any

from app.services.document_extraction.index_quality import identity_key
from app.services.document_extraction.index_schema import IndexedTradeline, ReportIndex
from app.services.document_extraction.page_bundle import select_pages

# Four detailed tradelines ≈ 8,500 output tokens: about a quarter of a 32k
# budget, so reasoning has room even on a dense collection account.
DEFAULT_BATCH_SIZE = 4
# One page either side of what the index named, as a safety margin for a
# tradeline that spills past where the index said it ended.
DEFAULT_CONTEXT_PAGES = 1


@dataclass(frozen=True)
class BatchPlan:
    """One unit of detailed extraction."""

    batch_id: str
    ordinal: int
    tradelines: tuple[IndexedTradeline, ...]
    # Original 1-based pages, ascending. What the bundle will contain.
    pages: tuple[int, ...]
    padding: tuple[int, ...]

    @property
    def size(self) -> int:
        return len(self.tradelines)

    @property
    def names(self) -> list[str]:
        return [t.creditor_name for t in self.tradelines]

    @property
    def keys(self) -> list[list[str]]:
        """Identity of each tradeline, so a checkpoint can be proven to belong
        to the batch being asked for rather than to a renumbered one."""
        return [list(identity_key(t)) for t in self.tradelines]

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "ordinal": self.ordinal,
            "size": self.size,
            "names": self.names,
            "keys": self.keys,
            "pages": list(self.pages),
            "padding": list(self.padding),
        }

    def manifest(self) -> str:
        """The tradelines this batch is for, as the model is told about them.

        Identity only — name, original creditor, masked number. Deliberately
        no page numbers: the model is looking at a bundle whose pages are
        renumbered, and telling it original page numbers would invite it to
        report those instead of what it sees. Page references are translated
        deterministically afterwards."""
        lines = []
        for i, tradeline in enumerate(self.tradelines, 1):
            parts = [f'{i}. "{tradeline.creditor_name}"']
            if tradeline.original_creditor:
                parts.append(f'original creditor "{tradeline.original_creditor}"')
            if tradeline.account_number:
                parts.append(f"account number {tradeline.account_number}")
            if tradeline.account_type:
                parts.append(f"({tradeline.account_type})")
            lines.append(", ".join(parts))
        return "\n".join(lines)


def plan_batches(
    index: ReportIndex,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    context_pages: int = DEFAULT_CONTEXT_PAGES,
) -> list[BatchPlan]:
    """Split an index into batches, in index order.

    Index order is kept rather than, say, grouping by page: it makes batch ids
    stable and predictable for an operator running them one at a time, and the
    index already lists tradelines roughly in document order.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    tradelines = list(index.tradelines or [])
    if not tradelines:
        return []

    total_pages = index.total_pages or max(
        (p for t in tradelines for p in (t.source_pages or [])), default=0
    )
    if total_pages < 1:
        raise ValueError("the index records no page numbers, so batches cannot be built")

    plans: list[BatchPlan] = []
    for ordinal, start in enumerate(range(0, len(tradelines), batch_size)):
        chunk = tuple(tradelines[start:start + batch_size])
        pages, padding = select_pages(
            (t.source_pages for t in chunk),
            total_pages=total_pages,
            context_pages=context_pages,
        )
        plans.append(BatchPlan(
            batch_id=f"b{ordinal}", ordinal=ordinal, tradelines=chunk,
            pages=pages, padding=padding,
        ))
    return plans


def plan_summary(plans: list[BatchPlan], index: ReportIndex) -> dict[str, Any]:
    """An operator-readable view of the whole plan, without running anything."""
    total = index.total_pages or 0
    bundled = sum(len(p.pages) for p in plans)
    return {
        "batches": [p.to_dict() for p in plans],
        "tradelines": sum(p.size for p in plans),
        "total_pages": total,
        "pages_sent": bundled,
        # The point of bundling: how much document we avoid re-sending versus
        # handing the whole report to every batch.
        "pages_if_whole_document": total * len(plans) if total else None,
        "pages_saved": (total * len(plans) - bundled) if total else None,
    }
