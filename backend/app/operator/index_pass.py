"""
Stage 1 validation: run the INDEX PASS ONLY against a real report.

    # against the stored original for an existing report (banks the index)
    DATABASE_URL=... python -m app.operator.index_pass --report <report_id> --expect 15

    # against a local PDF, no database involvement
    python -m app.operator.index_pass --pdf experian.pdf --expect 15

    # if Luna fails the quality gate, escalate the INDEX (not extraction)
    python -m app.operator.index_pass --report <id> --expect 15 --model gpt-5.6-terra

Defaults to gpt-5.6-luna at detail=low. Runs exactly one model call. It cannot
reach the extraction or audit tiers, and it refuses to run a Sol model unless
forced, because Sol is the escalation target for detailed extraction — not for
indexing.

Prints model, input/output/reasoning tokens, latency, estimated cost, the
indexed tradeline count, and the tradelines themselves so their identities and
page locations can be checked against the document by eye.

Exit code 0 if the index passes its quality gate, 1 if it does not.
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
# Sol is the detailed-extraction escalation target. Indexing it would spend
# the expensive model on the one pass that does not need it.
SOL_MODELS = ("gpt-5.6-sol",)


class _Collector:
    def __init__(self):
        self.records: list[UsageRecord] = []

    async def __call__(self, record: UsageRecord) -> None:
        self.records.append(record)


def _print_telemetry(record: UsageRecord | None, listed: int) -> None:
    print(f"\n{RULE}\nTELEMETRY\n{RULE}")
    if record is None:
        print("  no usage recorded (the call never reached the provider)")
        return
    total = (record.input_tokens or 0) + (record.output_tokens or 0)
    cost = record.estimated_cost_usd
    if cost is None:
        cost = estimate_cost_usd(record.model, record.input_tokens or 0, record.output_tokens or 0)
    rows = [
        ("model", record.model),
        ("succeeded", record.success),
        ("input tokens", f"{record.input_tokens or 0:,}"),
        ("output tokens", f"{record.output_tokens or 0:,}"),
        ("reasoning tokens", (record.context or {}).get("reasoning_tokens", "not reported")),
        ("cached input tokens", f"{record.cache_read_tokens or 0:,}"),
        ("total tokens", f"{total:,}"),
        ("latency", f"{(record.latency_ms or 0) / 1000:.1f}s"),
        ("estimated cost", f"${cost:.4f}" if cost is not None else "unknown model pricing"),
        ("indexed tradelines", listed),
        ("response id", (record.context or {}).get("response_id", "not reported")),
    ]
    for name, value in rows:
        print(f"  {name:<22} {value}")
    if listed and record.output_tokens:
        print(f"  {'output tokens each':<22} ~{record.output_tokens / listed:,.0f}")
    if record.error:
        print(f"  {'error':<22} {record.error[:200]}")


def _print_index(result) -> None:
    index = result.index
    print(f"\n{RULE}\nREPORT\n{RULE}")
    print(f"  bureau                 {index.bureau}")
    print(f"  document created       {index.document_created_date}")
    print(f"  report date            {index.report_date}")
    print(f"  score                  {index.score} ({index.score_type})")
    print(f"  declared tradelines    {index.tradeline_count}")
    print(f"  listed tradelines      {len(index.tradelines)}")
    print(f"  total pages            {index.total_pages}")
    if index.unreadable_pages:
        print(f"  unreadable pages       {sorted(index.unreadable_pages)}")

    print(f"\n{RULE}\nTRADELINES  (check these against the document)\n{RULE}")
    print(f"  {'#':>2}  {'creditor':<28} {'orig. creditor':<24} {'number':<20} pages")
    for i, t in enumerate(index.tradelines, 1):
        print(f"  {i:>2}  {(t.creditor_name or '')[:28]:<28} "
              f"{(t.original_creditor or '—')[:24]:<24} "
              f"{(t.account_number or '—')[:20]:<20} {t.source_pages}")


def _print_gate(quality, expected: int | None) -> None:
    print(f"\n{RULE}\nQUALITY GATE\n{RULE}")
    print(f"  listed {quality.listed}   declared {quality.declared}   "
          f"distinct {quality.distinct}   with page refs {quality.with_pages}")
    if expected is not None:
        print(f"  expected {expected}")
    if quality.ok:
        print("\n  PASS — the index is good enough to drive batched extraction.")
    else:
        print("\n  FAIL")
        for reason in quality.reasons:
            print(f"    · {reason}")
        print("\n  Next step: re-run the INDEX ONLY with a stronger model,")
        print("    python -m app.operator.index_pass ... --model gpt-5.6-terra")
        print("  Do not escalate to Sol, and do not run account batches.")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--report", help="an existing report id; reads its stored original")
    source.add_argument("--pdf", help="a local PDF; nothing is written to the database")
    parser.add_argument("--model", default="gpt-5.6-luna", help="index model (default gpt-5.6-luna)")
    parser.add_argument("--detail", default="low", choices=("low", "high"))
    parser.add_argument("--expect", type=int, help="tradeline count a human confirmed")
    parser.add_argument("--allow-sol", action="store_true",
                        help="permit a Sol model for indexing (normally refused)")
    parser.add_argument("--bank-failed", action="store_true",
                        help="checkpoint the index even if it fails the gate (inspection only)")
    parser.add_argument("--force", action="store_true",
                        help="re-index even though a passing index is already banked "
                             "(this spends money; by default a banked index is reused)")
    parser.add_argument("--json", dest="as_json", help="also write the full result here")
    args = parser.parse_args(argv)

    if any(s in args.model for s in SOL_MODELS) and not args.allow_sol:
        print(f"refusing to index with {args.model}: Sol is the escalation target for detailed\n"
              f"extraction, not for indexing. Use gpt-5.6-luna, then gpt-5.6-terra if the gate\n"
              f"fails. Pass --allow-sol only if you have decided otherwise.", file=sys.stderr)
        return 2
    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set", file=sys.stderr)
        return 2
    if args.report and not os.getenv("DATABASE_URL"):
        print("DATABASE_URL is not set (needed to read the stored original)", file=sys.stderr)
        return 2

    # Point the index tier at the requested model for this run only.
    settings.ai_document_index_model = args.model
    settings.document_index_detail = args.detail

    print(f"{RULE}")
    print(f"INDEX PASS ONLY — one call, {args.model} at detail={args.detail}")
    print("no extraction pass, no audit pass, no account batches")
    print(RULE)

    collector = _Collector()
    with only_usage_listener(collector):
        if args.pdf:
            from app.services.index_job import index_document

            document = Path(args.pdf).read_bytes()
            if not document.startswith(b"%PDF-"):
                print(f"{args.pdf}: not a PDF", file=sys.stderr)
                return 2
            print(f"  source: {args.pdf} ({len(document) / 1024:.0f} KB)")
            result = await index_document(document, filename=Path(args.pdf).name,
                                          expected_tradelines=args.expect)
        else:
            from app.services.index_job import index_report

            print(f"  source: stored original for report {args.report}")
            result = await index_report(args.report, expected_tradelines=args.expect,
                                        force=args.force)
            if result.reused:
                print("  REUSED the banked index — no model call, nothing spent.")
                print("  Pass --force to re-index deliberately.")

    record = next((r for r in collector.records if r.task == "index_report_document"), None)

    if result.index is None:
        failure = result.failure
        print(f"\n{RULE}\nINDEX PASS FAILED\n{RULE}")
        if failure:
            print(f"  {failure.error_class} -> {failure.status.value}")
            print(f"  {failure.detail[:400]}")
            print(f"  billed: {failure.billed}")
        _print_telemetry(record, 0)
        return 1

    _print_index(result)
    _print_telemetry(record, result.quality.listed)
    _print_gate(result.quality, args.expect)
    if args.report:
        print(f"\n  index checkpointed on the report: {result.banked}"
              f"{' (reused, not re-purchased)' if result.reused else ''}")

    if args.as_json:
        Path(args.as_json).write_text(json.dumps(result.to_dict(), indent=2, default=str))
        print(f"  full result written to {args.as_json}")

    return 0 if result.quality.ok else 1


def run(argv: list[str] | None = None) -> int:
    """Entry point shared with the repository's scripts/ wrapper."""
    return asyncio.run(main(argv))


if __name__ == "__main__":
    raise SystemExit(run())
