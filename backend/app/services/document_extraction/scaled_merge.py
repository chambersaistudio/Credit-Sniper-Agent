"""
Stage 3 of scaled extraction: deterministic merge, no provider call.

The Stage-1 index owns report-level facts and ordering. Stage-2 batch
checkpoints own detailed tradelines. This module refuses incomplete/stale
checkpoints, then assembles the exact CreditReportExtraction shape the
existing audit/reconciliation/persistence path already understands.
"""
from __future__ import annotations

from typing import Any

from app.services.batch_job import plan_fingerprint
from app.services.document_extraction.batch_schema import TradelineBatch
from app.services.document_extraction.batching import plan_batches
from app.services.document_extraction.index_schema import ReportIndex
from app.services.document_extraction.schema import (
    CreditReportExtraction, ExtractedInquiry, PublicRecord, SummaryMetric,
)


class ScaledMergeError(ValueError):
    """The checkpoint is not complete/consistent enough to merge."""


def _norm(value: str | None) -> str:
    return " ".join((value or "").split()).strip().lower()


def merge_scaled_checkpoint(checkpoint: dict[str, Any]) -> CreditReportExtraction:
    stored_index = checkpoint.get("index")
    if not stored_index:
        raise ScaledMergeError("scaled merge requires a banked Stage-1 index")
    index = ReportIndex.model_validate(stored_index)
    plans = plan_batches(index)
    banked = checkpoint.get("batches") or {}

    accounts = []
    unreadable = set(index.unreadable_pages or [])

    for plan in plans:
        entry = banked.get(plan.batch_id)
        if not entry:
            raise ScaledMergeError(f"missing banked batch {plan.batch_id}")
        if entry.get("plan_fingerprint") != plan_fingerprint(plan):
            raise ScaledMergeError(f"banked batch {plan.batch_id} belongs to a different index plan")
        quality = entry.get("quality") or {}
        if not quality.get("ok"):
            raise ScaledMergeError(f"banked batch {plan.batch_id} did not pass its quality gate")

        batch = TradelineBatch.model_validate(entry.get("batch") or {})
        if batch.missing_tradelines:
            raise ScaledMergeError(
                f"banked batch {plan.batch_id} still reports missing tradelines"
            )
        accounts.extend(batch.accounts)
        unreadable.update(batch.unreadable_pages or [])

    expected = index.tradeline_count if index.tradeline_count is not None else len(index.tradelines)
    if len(accounts) != len(index.tradelines) or len(accounts) != expected:
        raise ScaledMergeError(
            f"merged {len(accounts)} tradelines but index requires {expected}"
        )

    seen: set[tuple[str, str]] = set()
    duplicates: list[str] = []
    for account in accounts:
        if not account.account_number:
            continue
        key = (_norm(account.creditor_name), _norm(account.account_number))
        if key in seen:
            duplicates.append(account.creditor_name)
        seen.add(key)
    if duplicates:
        raise ScaledMergeError(
            "duplicate tradelines after merge: " + ", ".join(sorted(set(duplicates)))
        )

    inquiries = [
        ExtractedInquiry(
            creditor_name=i.creditor_name,
            inquiry_date=i.inquiry_date,
            inquiry_type=i.inquiry_type,
            inquiry_category=i.inquiry_category,
            business_type=i.business_type,
            contact=None,
            source_pages=i.source_pages,
        )
        for i in index.inquiries
    ]
    public_records = [
        PublicRecord(
            record_type=r.record_type,
            status=r.status,
            filed_date=r.filed_date,
            amount=r.amount,
            reference=r.reference,
            source_pages=r.source_pages,
        )
        for r in index.public_records
    ]

    return CreditReportExtraction(
        bureau=index.bureau,
        report_date=index.report_date,
        document_created_date=index.document_created_date,
        consumer_on_file_since=index.consumer_on_file_since,
        score_type=index.score_type,
        score=index.score,
        summary_metrics=[SummaryMetric(name=m.name, value=m.value) for m in index.summary_metrics],
        accounts=accounts,
        inquiries=inquiries,
        public_records=public_records,
        unreadable_pages=sorted(unreadable),
        warnings=[],
    )


def scaled_model_label(checkpoint: dict[str, Any]) -> str:
    models = sorted({
        (entry or {}).get("model")
        for entry in ((checkpoint.get("batches") or {}).values())
        if (entry or {}).get("model")
    })
    return "scaled:" + ("+".join(models) if models else "unknown")
