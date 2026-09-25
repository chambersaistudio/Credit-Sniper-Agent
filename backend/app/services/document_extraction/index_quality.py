"""
The quality gate for a report index.

Deterministic, and strict about the two things an index exists to guarantee:
that every tradeline in the document is listed exactly once, and that each one
can be found again. Everything downstream inherits these — a tradeline missing
from the index is missing from every batch, and one with no page reference
cannot be read in detail or audited afterwards.

Nothing here calls a model.
"""
import re
from dataclasses import dataclass, field
from typing import Any

from app.services.document_extraction.index_schema import IndexedTradeline, ReportIndex

SUPPORTED_BUREAUS = {"equifax", "experian", "transunion"}


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip().lower()


def identity_key(tradeline: IndexedTradeline) -> tuple[str, str, str]:
    """How two index entries are told apart.

    Creditor name, the last four digits of the masked number, and the original
    creditor — because a report legitimately lists the same furnisher twice
    (two Navy Federal accounts), and a collection agency legitimately appears
    twice under different original creditors (Jefferson Capital for Mission
    Lane and for T-Mobile). Name alone would merge those; the masked number
    alone is unsafe, since TransUnion warns it may be scrambled."""
    digits = re.sub(r"\D", "", tradeline.account_number or "")
    return (_norm(tradeline.creditor_name), digits[-4:], _norm(tradeline.original_creditor))


@dataclass
class IndexQuality:
    ok: bool
    listed: int
    declared: int | None
    distinct: int
    with_pages: int
    reasons: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    missing_pages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "listed": self.listed, "declared": self.declared,
            "distinct": self.distinct, "with_pages": self.with_pages,
            "reasons": self.reasons, "duplicates": self.duplicates,
            "missing_pages": self.missing_pages,
        }


def assess_index(index: ReportIndex | None, *, expected_tradelines: int | None = None) -> IndexQuality:
    """Is this index good enough to drive detailed extraction?

    `expected_tradelines` is for validation against a report whose true count
    a human has confirmed. In production nothing knows the true count, so the
    model's own declared total is the only cross-check available — which is
    precisely why the schema asks for it separately.
    """
    if index is None:
        return IndexQuality(ok=False, listed=0, declared=None, distinct=0, with_pages=0,
                            reasons=["No index was produced."])

    tradelines = index.tradelines or []
    reasons: list[str] = []

    seen: dict[tuple[str, str, str], int] = {}
    duplicates: list[str] = []
    for tradeline in tradelines:
        key = identity_key(tradeline)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] == 2:
            duplicates.append(tradeline.creditor_name)

    unnamed = [t for t in tradelines if not _norm(t.creditor_name)]
    missing_pages = [t.creditor_name for t in tradelines if not t.source_pages]

    if not tradelines:
        reasons.append("The index lists no tradelines.")
    if unnamed:
        reasons.append(f"{len(unnamed)} tradeline(s) have no creditor name.")
    if duplicates:
        reasons.append(f"Tradelines listed more than once: {', '.join(sorted(set(duplicates)))}.")
    if missing_pages:
        # Without a page reference the entry cannot be batched or audited.
        reasons.append(f"{len(missing_pages)} tradeline(s) have no source page.")
    if index.tradeline_count is not None and index.tradeline_count != len(tradelines):
        # The listing disagreeing with the model's own count is the signature
        # of a truncated or abandoned list — the failure that would silently
        # drop accounts from every later batch.
        reasons.append(
            f"The index declares {index.tradeline_count} tradelines but lists {len(tradelines)}."
        )
    if _norm(index.bureau) not in SUPPORTED_BUREAUS:
        reasons.append(f"Bureau not identified (got {index.bureau!r}).")
    if index.unreadable_pages:
        reasons.append(f"Pages could not be read reliably: {sorted(index.unreadable_pages)}.")
    if expected_tradelines is not None and len(tradelines) != expected_tradelines:
        reasons.append(
            f"Expected {expected_tradelines} tradelines, indexed {len(tradelines)}."
        )

    return IndexQuality(
        ok=not reasons,
        listed=len(tradelines),
        declared=index.tradeline_count,
        distinct=len(seen),
        with_pages=len(tradelines) - len(missing_pages),
        reasons=reasons,
        duplicates=sorted(set(duplicates)),
        missing_pages=missing_pages,
    )
