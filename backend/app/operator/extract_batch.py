"""
Stage 2: run ONE batch of detailed tradeline extraction.

    # see the plan without spending anything
    DATABASE_URL=... python -m app.operator.extract_batch --report <id> --plan

    # run a single batch
    DATABASE_URL=... OPENAI_API_KEY=... \
      python -m app.operator.extract_batch --report <id> --batch b0

Each batch is up to four tradelines named by the banked Stage-1 index, read
from a transient PDF holding only the pages that index says they live on. The
original in R2 is read, never modified. Page numbers in the result are
ORIGINAL page numbers, translated deterministically from the bundle — the
model is never asked where its pages sat in the real report.

One batch per invocation, by design: there is no flag that runs them all, so
no accident can spend four times what was asked for. A batch that already
passed is reused rather than re-read unless --force is given.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from app.config import settings  # noqa: E402
from app.services.ai import UsageRecord, only_usage_listener  # noqa: E402
from app.services.ai.config import estimate_cost_usd  # noqa: E402

RULE = "─" * 78


class _Collector:
    def __init__(self):
        self.records: list[UsageRecord] = []

    async def __call__(self, record: UsageRecord) -> None:
        self.records.append(record)


async def _load_report(report_id):
    from app.database import async_session_maker
    from app.models.credit_report import CreditReport

    async with async_session_maker() as db:
        report = await db.get(CreditReport, report_id)
        if report is None:
            raise SystemExit(f"no report {report_id}")
        return report


def _print_plan(report) -> None:
    from app.services.batch_job import banked_index, batch_status

    index = banked_index(report)
    rows = batch_status(report)
    print(f"{RULE}\nBATCH PLAN  ({len(index.tradelines)} indexed tradelines, "
          f"{index.total_pages} page document)\n{RULE}")
    pages_sent = 0
    for row in rows:
        pages_sent += len(row["pages"])
        mark = "banked" if row["banked"] else "      "
        pad = f" (+{len(row['padding'])} margin)" if row["padding"] else ""
        print(f"  {row['batch_id']:<4} {mark}  pages {row['pages']}{pad}")
        for name in row["names"]:
            print(f"          · {name}")
        if row["banked"]:
            print(f"          banked {row['extracted_at']} by {row['model']}")
    whole = (index.total_pages or 0) * len(rows)
    print(f"\n  pages sent across all batches: {pages_sent}")
    print(f"  if each batch re-sent the whole report: {whole}")
    if whole:
        print(f"  saved by bundling: {whole - pages_sent} page-sends "
              f"({(whole - pages_sent) / whole * 100:.1f}%)")


def _print_result(result) -> None:
    print(f"\n{RULE}\nBATCH {result.plan.batch_id}\n{RULE}")
    bundle = result.bundle
    if bundle:
        print(f"  bundle: {bundle.page_count} page(s) from the original — "
              f"{list(bundle.pages)}")
        if bundle.padding:
            print(f"  safety margin pages: {list(bundle.padding)}")
        print(f"  bundle page -> original: "
              + ", ".join(f"{i + 1}→{p}" for i, p in enumerate(bundle.pages)))
    if result.remap:
        print(f"  page references remapped: {result.remap.remapped}"
              + (f", unmapped: {result.remap.unmapped}" if result.remap.unmapped else ""))

    print(f"\n  {'tradeline':<28} {'number':<20} {'balance':<12} {'status':<22} pages")
    for account in result.accounts:
        print(f"  {(account.creditor_name or '')[:28]:<28} "
              f"{(account.account_number or '—')[:20]:<20} "
              f"{(account.balance or '—')[:12]:<12} "
              f"{(account.status_raw or '—')[:22]:<22} {account.source_pages}")
        months = len(account.payment_history or [])
        evidence = len(account.field_evidence or [])
        print(f"      {months} payment-history month(s), {evidence} evidence item(s)")


def _print_telemetry(record: UsageRecord | None, accounts: int) -> None:
    print(f"\n{RULE}\nTELEMETRY\n{RULE}")
    if record is None:
        print("  no usage recorded (no model call was made)")
        return
    cost = record.estimated_cost_usd
    if cost is None:
        cost = estimate_cost_usd(record.model, record.input_tokens or 0, record.output_tokens or 0)
    rows = [
        ("model", record.model),
        ("succeeded", record.success),
        ("input tokens", f"{record.input_tokens or 0:,}"),
        ("output tokens", f"{record.output_tokens or 0:,}"),
        ("reasoning tokens", (record.context or {}).get("reasoning_tokens", "not reported")),
        ("latency", f"{(record.latency_ms or 0) / 1000:.1f}s"),
        ("estimated cost", f"${cost:.4f}" if cost is not None else "unknown model pricing"),
        ("tradelines returned", accounts),
        ("response id", (record.context or {}).get("response_id", "not reported")),
    ]
    for name, value in rows:
        print(f"  {name:<22} {value}")
    if accounts and record.output_tokens:
        print(f"  {'output tokens each':<22} ~{record.output_tokens / accounts:,.0f}")
    if record.error:
        print(f"  {'error':<22} {record.error[:200]}")


def _print_gate(quality) -> None:
    print(f"\n{RULE}\nQUALITY GATE\n{RULE}")
    print(f"  asked {quality.asked}   returned {quality.returned}   matched {quality.matched}")
    if quality.ok:
        print("\n  PASS — this batch is banked and will not be read again.")
    else:
        print("\n  FAIL — not banked.")
        for reason in quality.reasons:
            print(f"    · {reason}")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", required=True, help="a report with a banked Stage-1 index")
    parser.add_argument("--batch", help="which batch to run, e.g. b0")
    parser.add_argument("--plan", action="store_true", help="show the plan and exit, spending nothing")
    parser.add_argument("--model", help="override the extraction model for this run")
    parser.add_argument("--detail", default="high", choices=("low", "high"))
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--context-pages", type=int, default=1,
                        help="neighbouring pages included as a safety margin (default 1)")
    parser.add_argument("--force", action="store_true",
                        help="re-run a batch that is already banked (this spends money)")
    parser.add_argument("--bank-failed", action="store_true",
                        help="checkpoint the batch even if it fails the gate (inspection only)")
    parser.add_argument("--json", dest="as_json", help="also write the full result here")
    args = parser.parse_args(argv)

    if not os.getenv("DATABASE_URL"):
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    from app.services.batch_job import NoBankedIndex, batch_plans, run_report_batch

    report = await _load_report(args.report)
    try:
        plans = batch_plans(report, batch_size=args.batch_size, context_pages=args.context_pages)
    except NoBankedIndex as e:
        print(f"{e}", file=sys.stderr)
        return 2

    if args.plan or not args.batch:
        _print_plan(report)
        if not args.batch:
            print(f"\n  choose one with --batch {plans[0].batch_id}")
        return 0

    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set", file=sys.stderr)
        return 2
    if args.model:
        settings.ai_document_extraction_model = args.model
    settings.document_extraction_detail = args.detail

    from app.services.ai.config import ModelTier, resolve_tier

    model = resolve_tier(ModelTier.DOCUMENT_EXTRACTION).model
    print(RULE)
    print(f"ONE BATCH — {args.batch} of {[p.batch_id for p in plans]}, "
          f"{model} at detail={args.detail}")
    print("no other batch is touched by this command")
    print(RULE)

    collector = _Collector()
    with only_usage_listener(collector):
        result = await run_report_batch(
            args.report, args.batch, batch_size=args.batch_size,
            context_pages=args.context_pages, force=args.force, bank_failed=args.bank_failed,
        )

    if result.reused:
        print(f"\n  batch {args.batch} is already banked — reused, nothing spent.")
        print("  Pass --force to re-read it deliberately.")
        _print_result(result)
        return 0

    record = next((r for r in collector.records if r.task == "extract_tradeline_batch"), None)

    if result.batch is None:
        failure = result.failure
        print(f"\n{RULE}\nBATCH FAILED\n{RULE}")
        if failure:
            print(f"  {failure.error_class} -> {failure.status.value}")
            print(f"  {failure.detail[:400]}")
            print(f"  billed: {failure.billed}")
        _print_telemetry(record, 0)
        print("\n  Other batches are unaffected and remain banked.")
        return 1

    _print_result(result)
    _print_telemetry(record, len(result.accounts))
    _print_gate(result.quality)
    print(f"\n  checkpointed: {result.banked}")

    if args.as_json:
        Path(args.as_json).write_text(json.dumps(result.to_dict(), indent=2, default=str))
        print(f"  full result written to {args.as_json}")

    return 0 if result.quality.ok else 1


def run(argv: list[str] | None = None) -> int:
    """Entry point shared with the repository's scripts/ wrapper."""
    return asyncio.run(main(argv))


if __name__ == "__main__":
    raise SystemExit(run())
