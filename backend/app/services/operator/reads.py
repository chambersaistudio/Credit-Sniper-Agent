"""
The free operations: everything an operator can learn without spending.

All of these read state we already store. None calls a provider, and none
returns document content — the checkpoint views deliberately report shape
(how many accounts, how many months) rather than contents, which is both
safer and what an operator actually needs to see on a phone.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_usage import AIUsageLog
from app.models.credit_report import CreditReport
from app.services.batch_job import NoBankedIndex, batch_plans, batch_status
from app.services.document_extraction.index_quality import assess_index
from app.services.document_extraction.index_schema import ReportIndex
from app.services.document_extraction.status import ExtractionStatus

# Stages that mean a report is still being worked on.
PENDING = ("queued", "extracting", "extraction_complete", "auditing", "reconciling")


async def get_report(db: AsyncSession, report_id) -> CreditReport:
    report = await db.get(CreditReport, report_id)
    if report is None:
        raise LookupError(f"no report {report_id}")
    return report


def extraction_state(report: CreditReport) -> dict[str, Any]:
    """The state of one report, in operator terms."""
    checkpoint = report.extraction_checkpoint or {}
    parsed = report.parsed_data or {}
    try:
        status = ExtractionStatus(report.extraction_status)
        retryable, billed, operational = status.is_retryable, status.was_billed, status.is_operational
    except ValueError:
        retryable = billed = operational = None
    return {
        "report_id": str(report.id),
        "bureau": report.bureau,
        "created_at": report.created_at.isoformat() if report.created_at else None,
        "processing_stage": report.processing_stage,
        "processing": report.processing_stage in PENDING,
        "extraction_status": report.extraction_status,
        "retryable": retryable,
        "was_billed": billed,
        "operational_failure": operational,
        "attempt_count": report.attempt_count,
        # The exception class, which is operator-only by design and never
        # reaches a consumer endpoint.
        "last_processing_error_class": report.last_processing_error_class,
        "account_count": parsed.get("account_count"),
        "has_index": bool(checkpoint.get("index")),
        "has_extraction": bool(checkpoint.get("extraction")),
        "banked_batches": sorted((checkpoint.get("batches") or {}).keys()),
        "document_sha256": report.document_sha256,
    }


async def recent_reports(db: AsyncSession, limit: int = 20) -> list[dict[str, Any]]:
    rows = (await db.execute(
        select(CreditReport).order_by(CreditReport.created_at.desc()).limit(limit)
    )).scalars().all()
    return [extraction_state(report) for report in rows]


def inspect_checkpoint(report: CreditReport) -> dict[str, Any]:
    """What each checkpoint holds, by shape. Never contents.

    An operator needs to know whether a pass is banked and how big it is, not
    what the consumer's accounts say — and shape is safe to render anywhere."""
    checkpoint = report.extraction_checkpoint or {}
    out: dict[str, Any] = {"report_id": str(report.id), "keys": sorted(checkpoint)}

    index = checkpoint.get("index")
    if index:
        tradelines = index.get("tradelines") or []
        out["index"] = {
            "model": checkpoint.get("index_model"),
            "banked_at": checkpoint.get("indexed_at"),
            "tradelines": len(tradelines),
            "declared_count": index.get("tradeline_count"),
            "total_pages": index.get("total_pages"),
            "bureau": index.get("bureau"),
            "quality": checkpoint.get("index_quality"),
            "with_page_refs": sum(1 for t in tradelines if t.get("source_pages")),
        }

    extraction = checkpoint.get("extraction")
    if extraction:
        accounts = extraction.get("accounts") or []
        size = len(json.dumps(extraction))
        out["extraction"] = {
            "model": checkpoint.get("extractor_model"),
            "accounts": len(accounts),
            "payment_history_months": sum(len(a.get("payment_history") or []) for a in accounts),
            "serialized_chars": size,
            "approx_output_tokens": round(size / 3.6),
        }

    batches = checkpoint.get("batches") or {}
    if batches:
        out["batches"] = {
            batch_id: {
                "model": entry.get("model"),
                "accounts": len((entry.get("batch") or {}).get("accounts") or []),
                "pages": (entry.get("bundle") or {}).get("pages"),
                "extracted_at": entry.get("extracted_at"),
                "quality_ok": (entry.get("quality") or {}).get("ok"),
            }
            for batch_id, entry in sorted(batches.items())
        }

    audit_failure = (report.extraction_audit or {}).get("failure")
    if audit_failure:
        out["last_failure"] = audit_failure
    return out


def batch_plan(report: CreditReport, *, batch_size: int = 4,
               context_pages: int = 1) -> dict[str, Any]:
    """The batches this report's banked index produces, and which are banked."""
    rows = batch_status(report, batch_size=batch_size, context_pages=context_pages)
    index = ReportIndex.model_validate((report.extraction_checkpoint or {})["index"])
    total = index.total_pages or 0
    sent = sum(len(r["pages"]) for r in rows)
    whole = total * len(rows) if total else None
    return {
        "report_id": str(report.id),
        "total_pages": total,
        "indexed_tradelines": len(index.tradelines),
        "index_quality": assess_index(index).to_dict(),
        "batches": rows,
        "pages_sent": sent,
        "pages_if_whole_document": whole,
        "pages_saved": (whole - sent) if whole else None,
        "reduction_pct": round((whole - sent) / whole * 100, 1) if whole else None,
    }


async def diagnose(db: AsyncSession, report: CreditReport) -> dict[str, Any]:
    """Processing history, the classified failure, checkpoint shape and the AI
    usage attributable to this report — the same picture the command-line
    diagnosis prints, as data."""
    usage = (await db.execute(
        select(AIUsageLog)
        .where(AIUsageLog.context["report_id"].astext == str(report.id))
        .order_by(AIUsageLog.created_at)
    )).scalars().all()

    wall_clock = None
    if report.processing_started_at and report.processing_finished_at:
        wall_clock = (report.processing_finished_at - report.processing_started_at).total_seconds()

    return {
        **extraction_state(report),
        "processing_started_at": report.processing_started_at.isoformat() if report.processing_started_at else None,
        "processing_finished_at": report.processing_finished_at.isoformat() if report.processing_finished_at else None,
        "wall_clock_seconds": wall_clock,
        "checkpoint": inspect_checkpoint(report),
        "provider_error": (report.extraction_audit or {}).get("provider_error"),
        "ai_usage": [
            {
                "task": row.task,
                "model": row.model,
                "success": row.success,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "latency_ms": row.latency_ms,
                "estimated_cost_usd": row.estimated_cost_usd,
                "error": row.error,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "reasoning_tokens": (row.context or {}).get("reasoning_tokens"),
                "max_output_tokens": (row.context or {}).get("max_output_tokens"),
                "response_id": (row.context or {}).get("response_id"),
            }
            for row in usage
        ],
        "total_cost_usd": round(sum(r.estimated_cost_usd or 0.0 for r in usage), 6) or None,
    }


__all__ = ["NoBankedIndex", "batch_plan", "batch_plans", "diagnose", "extraction_state",
           "get_report", "inspect_checkpoint", "recent_reports"]
