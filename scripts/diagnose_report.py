#!/usr/bin/env python3
"""
Operator diagnosis for one report's extraction, read-only.

    python scripts/diagnose_report.py <report_id>
    python scripts/diagnose_report.py --failed          # recent failures
    python scripts/diagnose_report.py --failed --limit 5

Prints the processing history, the classified failure, what each checkpoint
holds, and every AI usage row attributable to the report — including what a
failed pass was billed.

It never prints report contents. Checkpoints are summarized by shape (how many
accounts, how many payment-history months) rather than dumped, so this is safe
to run against production and paste into an issue.

Requires DATABASE_URL (the same value the API uses).
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sqlalchemy import select, text  # noqa: E402

from app.database import async_session_maker  # noqa: E402
from app.models.ai_usage import AIUsageLog  # noqa: E402
from app.models.credit_report import CreditReport  # noqa: E402
from app.services.ai.config import estimate_cost_usd  # noqa: E402
from app.services.document_extraction.status import ExtractionStatus  # noqa: E402

RULE = "─" * 78
# Every stage that is not one of these is terminal.
PENDING = ("queued", "extracting", "extraction_complete", "auditing", "reconciling")


def _fmt(value, default="—"):
    return default if value in (None, "") else value


def _summarize_checkpoint(checkpoint: dict | None) -> list[str]:
    """Shape only — never contents."""
    if not checkpoint:
        return ["  (none — no expensive pass has completed)"]
    lines = []
    extraction = checkpoint.get("extraction")
    if extraction:
        accounts = extraction.get("accounts") or []
        months = sum(len(a.get("payment_history") or []) for a in accounts)
        evidence = sum(len(a.get("field_evidence") or []) for a in accounts)
        size = len(json.dumps(extraction))
        lines += [
            f"  extraction   BANKED  ({checkpoint.get('extractor_model') or 'model unknown'})",
            f"    {len(accounts)} account(s), {months} payment-history month(s), "
            f"{evidence} evidence item(s), {len(extraction.get('inquiries') or [])} inquiry/ies",
            f"    serialized {size:,} chars ≈ {size / 3.6:,.0f} output tokens",
        ]
    else:
        lines.append("  extraction   NOT BANKED — pass 1 never succeeded")

    if checkpoint.get("audit_done"):
        verdict = "returned" if checkpoint.get("audit") else "FAILED"
        lines.append(f"  audit        {verdict}  ({checkpoint.get('auditor_model') or 'model unknown'})")
        failure = checkpoint.get("audit_failure")
        if failure:
            lines.append(f"    {failure.get('error_class')}: {failure.get('detail')}")
            if failure.get("billed"):
                lines.append(f"    BILLED {failure.get('input_tokens'):,} in / "
                             f"{failure.get('output_tokens'):,} out, budget "
                             f"{failure.get('max_output_tokens')}")
        elif checkpoint.get("audit_error"):
            lines.append(f"    {checkpoint['audit_error']}")
    else:
        lines.append("  audit        not run")
    return lines


async def _usage_rows(session, report: CreditReport):
    """Usage attributable to this report.

    Matched on context->>'report_id' (written by the background worker). Older
    rows predate that and are matched by user plus the processing window."""
    rows = (await session.execute(
        select(AIUsageLog).where(AIUsageLog.context["report_id"].astext == str(report.id))
        .order_by(AIUsageLog.created_at)
    )).scalars().all()
    if rows:
        return rows, "context.report_id"
    if not report.processing_started_at:
        return [], "none"
    rows = (await session.execute(
        select(AIUsageLog)
        .where(AIUsageLog.user_id == report.user_id,
               AIUsageLog.task.in_(("extract_report_document", "audit_report_document")),
               AIUsageLog.created_at >= report.processing_started_at)
        .order_by(AIUsageLog.created_at)
    )).scalars().all()
    return rows, "user_id + processing window (approximate)"


async def diagnose(session, report: CreditReport) -> None:
    stage = report.processing_stage
    status = report.extraction_status
    print(RULE)
    print(f"report {report.id}   bureau={_fmt(report.bureau)}   uploaded {report.created_at}")
    print(RULE)

    print("\nPROCESSING")
    print(f"  processing_stage            {stage}"
          f"{'  (still in flight)' if stage in PENDING else '  (terminal)'}")
    print(f"  extraction_status           {status}")
    try:
        parsed_status = ExtractionStatus(status)
        print(f"    retryable                 {parsed_status.is_retryable}")
        print(f"    was billed                {parsed_status.was_billed}")
        print(f"    operational (not the PDF) {parsed_status.is_operational}")
    except ValueError:
        print("    (status predates the current taxonomy)")
    print(f"  attempt_count               {report.attempt_count}")
    print(f"  last_processing_error_class {_fmt(report.last_processing_error_class)}")
    print(f"  started / finished          {_fmt(report.processing_started_at)} / "
          f"{_fmt(report.processing_finished_at)}")
    if report.processing_started_at and report.processing_finished_at:
        print(f"  wall clock                  "
              f"{(report.processing_finished_at - report.processing_started_at).total_seconds():.0f}s")
    print(f"  document_sha256             {_fmt(report.document_sha256)}")
    print(f"  storage_key present         {bool(report.storage_key)}")

    print("\nCHECKPOINTS (what a retry would NOT have to buy again)")
    for line in _summarize_checkpoint(report.extraction_checkpoint):
        print(line)

    audit = report.extraction_audit or {}
    print("\nFAILURE DETAIL (operator-only; never returned by an API endpoint)")
    print(f"  provider_error  {_fmt(audit.get('provider_error'))}")
    failure = audit.get("failure")
    if failure:
        print(f"  classified as   {failure.get('status')} (from {failure.get('error_class')})")
        print(f"  failed pass     {failure.get('pass')}")
        print(f"  billed          {failure.get('billed')}")
        if failure.get("billed"):
            print(f"    tokens        {failure.get('input_tokens'):,} in / "
                  f"{failure.get('output_tokens'):,} out"
                  + (f", {failure['reasoning_tokens']:,} reasoning"
                     if failure.get("reasoning_tokens") else ""))
            print(f"    budget        max_output_tokens={failure.get('max_output_tokens')}")
        print(f"  response_id     {_fmt(failure.get('response_id'))}")
        print(f"  latency         {failure.get('latency_ms')} ms")
    else:
        print("  (no structured failure recorded — this report predates the failure taxonomy,")
        print("   so read last_processing_error_class and provider_error above)")

    rows, how = await _usage_rows(session, report)
    print(f"\nAI USAGE ROWS  (matched by {how})")
    if not rows:
        print("  none found")
    else:
        print(f"  {'task':<26} {'model':<16} {'ok':<3} {'in':>9} {'out':>8} {'ms':>8} {'cost':>9}")
        total_in = total_out = 0
        total_cost = 0.0
        for row in rows:
            cost = row.estimated_cost_usd
            total_in += row.input_tokens or 0
            total_out += row.output_tokens or 0
            total_cost += cost or 0.0
            print(f"  {row.task:<26} {row.model:<16} {str(row.success):<3} "
                  f"{row.input_tokens or 0:>9,} {row.output_tokens or 0:>8,} "
                  f"{row.latency_ms or 0:>8,.0f} {('$%.4f' % cost) if cost else '—':>9}")
            if row.error:
                print(f"      error: {row.error[:160]}")
            for key in ("response_id", "max_output_tokens", "reasoning_tokens"):
                if (row.context or {}).get(key) is not None:
                    print(f"      {key}: {row.context[key]}")
        print(f"  {'TOTAL':<26} {'':<16} {'':<3} {total_in:>9,} {total_out:>8,} "
              f"{'':>8} {'$%.4f' % total_cost:>9}")
        if any((r.input_tokens or 0) == 0 and not r.success for r in rows):
            print("\n  NOTE: a failed row showing 0 tokens predates billed-usage capture.")
            print("  Its real spend is only in the provider's own usage dashboard.")

    print()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("report_id", nargs="?", help="the report to diagnose")
    parser.add_argument("--failed", action="store_true",
                        help="diagnose recent reports that did not reach verified")
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()

    if not os.getenv("DATABASE_URL"):
        print("DATABASE_URL is not set (use the same value the API uses)", file=sys.stderr)
        return 2
    if not args.report_id and not args.failed:
        parser.error("give a report id, or --failed")

    async with async_session_maker() as session:
        if args.report_id:
            report = await session.get(CreditReport, args.report_id)
            if report is None:
                print(f"no report {args.report_id}", file=sys.stderr)
                return 1
            reports = [report]
        else:
            reports = (await session.execute(
                select(CreditReport)
                .where(CreditReport.extraction_status != ExtractionStatus.VERIFIED.value)
                .order_by(CreditReport.created_at.desc())
                .limit(args.limit)
            )).scalars().all()
            if not reports:
                print("no unverified reports")
                return 0
        for report in reports:
            await diagnose(session, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
