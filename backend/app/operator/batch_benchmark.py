"""
Benchmark ONE batch across detailed-extraction models.

    DATABASE_URL=... OPENAI_API_KEY=... \
      python -m app.operator.batch_benchmark --report <id> --batch b0 \
        --truth batch0_truth.json

Runs the same batch — the same tradelines, the same page bundle — under:

    A  gpt-5.6-luna   detail=high
    B  gpt-5.6-terra  detail=high
    C  gpt-5.6-sol    detail=high

and scores each against confirmed ground truth: field-level accuracy,
payment-history accuracy, provenance accuracy (against ORIGINAL page numbers),
tokens, latency, cost and the batch quality gate.

It changes no production defaults and banks nothing: these are measurements,
not the report's extraction. The batch you bank is still whatever
`python -m app.operator.extract_batch` ran — and this command verifies that
the report's banked batches are byte-identical before and after it runs.

Ground truth is a JSON file holding the accounts for THIS batch only:

    {"accounts": [
       {"creditor_name": "ATLAS", "account_number": "5299XXXXXXXX1234",
        "balance": "$16", "open_closed": "Open", "date_opened": "Dec 22, 2025",
        "source_pages": [3],
        "payment_history": {"2026-05": "OK", "2026-04": "OK"}}
    ]}

Only the fields a truth file states are scored, so it can start with what
matters and grow. No consumer identity belongs in it.

Inside the deployed image, where writing a file is awkward, pass the same
JSON with --truth-inline '<json>' or pipe it in with --truth -.
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

# Exactly the configurations asked for: the same batch at full detail, so the
# only variable is the model.
CONFIGS = [
    ("A", "gpt-5.6-luna", "high"),
    ("B", "gpt-5.6-terra", "high"),
    ("C", "gpt-5.6-sol", "high"),
]


class _Collector:
    def __init__(self):
        self.records: list[UsageRecord] = []

    async def __call__(self, record: UsageRecord) -> None:
        self.records.append(record)


def _pct(tally) -> str:
    return "—" if tally.accuracy is None else f"{tally.accuracy * 100:.1f}%"


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", required=True)
    parser.add_argument("--batch", default="b0")
    truth_source = parser.add_mutually_exclusive_group(required=True)
    truth_source.add_argument("--truth", help="ground-truth JSON file, or - to read stdin")
    truth_source.add_argument("--truth-inline", help="ground-truth JSON as a literal string")
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--context-pages", type=int, default=1)
    parser.add_argument("--config", action="append", help="limit to these (A/B/C)")
    parser.add_argument("--json", dest="as_json")
    args = parser.parse_args(argv)

    if not os.getenv("DATABASE_URL") or not os.getenv("OPENAI_API_KEY"):
        print("DATABASE_URL and OPENAI_API_KEY must both be set", file=sys.stderr)
        return 2

    if args.truth_inline:
        raw, where = args.truth_inline, "--truth-inline"
    elif args.truth == "-":
        raw, where = sys.stdin.read(), "stdin"
    else:
        raw, where = Path(args.truth).read_text(), args.truth
    try:
        truth = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"{where}: not valid JSON ({e})", file=sys.stderr)
        return 2
    truth_accounts = truth.get("accounts") or []
    if not truth_accounts:
        print(f"{where}: no accounts", file=sys.stderr)
        return 2

    from app.database import async_session_maker
    from app.models.credit_report import CreditReport
    from app.services.batch_job import batch_plans, extract_batch
    from app.services.benchmark.batch_scoring import score_batch
    from app.services.storage import get_storage

    async with async_session_maker() as db:
        report = await db.get(CreditReport, args.report)
        if report is None:
            print(f"no report {args.report}", file=sys.stderr)
            return 2
        plans = batch_plans(report, batch_size=args.batch_size,
                            context_pages=args.context_pages)
        plan = next((p for p in plans if p.batch_id == args.batch), None)
        if plan is None:
            print(f"no batch {args.batch}; have {[p.batch_id for p in plans]}", file=sys.stderr)
            return 2
        document = await get_storage().get(report.storage_key)
        # Snapshotted so the "banks nothing" promise is checked, not just made.
        banked_before = json.dumps(
            (report.extraction_checkpoint or {}).get("batches") or {}, sort_keys=True
        )

    configs = [c for c in CONFIGS if not args.config or c[0] in args.config]
    print(RULE)
    print(f"BATCH BENCHMARK — {args.batch}: {', '.join(plan.names)}")
    print(f"pages {list(plan.pages)} of the original, {len(configs)} configs, "
          f"{len(configs)} model calls")
    print("nothing is banked; production defaults are unchanged")
    print(RULE)

    saved = (settings.ai_document_extraction_model, settings.document_extraction_detail)
    results = []
    try:
        for name, model, detail in configs:
            settings.ai_document_extraction_model = model
            settings.document_extraction_detail = detail
            collector = _Collector()
            print(f"\n  running {name}: {model} at detail={detail} …")
            with only_usage_listener(collector):
                result = await extract_batch(document, plan,
                                             context={"benchmark_batch": name})
            record = next((r for r in collector.records
                           if r.task == "extract_tradeline_batch"), None)
            card = score_batch(args.batch, name, result.accounts, truth_accounts,
                               result.quality)
            results.append((name, model, detail, card, record, result))
    finally:
        settings.ai_document_extraction_model, settings.document_extraction_detail = saved

    print(f"\n{RULE}\nQUALITY\n{RULE}")
    header = (f"  {'cfg':<4} {'model':<15} {'matched':<9} {'fields':<9} "
              f"{'payments':<9} {'provenance':<11} {'gate':<6}")
    print(header)
    for name, model, _, card, _, _ in results:
        print(f"  {name:<4} {model:<15} {f'{card.matched}/{card.asked}':<9} "
              f"{_pct(card.fields):<9} {_pct(card.payment_history):<9} "
              f"{_pct(card.provenance):<11} {'PASS' if card.gate_ok else 'FAIL':<6}")

    print(f"\n{RULE}\nCOST AND LATENCY\n{RULE}")
    print(f"  {'cfg':<4} {'model':<15} {'in':>9} {'out':>8} {'reasoning':>10} "
          f"{'latency':>9} {'cost':>9}")
    for name, model, _, card, record, _ in results:
        if record is None:
            print(f"  {name:<4} {model:<15} {'—':>9} {'—':>8} {'—':>10} {'—':>9} {'—':>9}")
            continue
        cost = record.estimated_cost_usd or estimate_cost_usd(
            record.model, record.input_tokens or 0, record.output_tokens or 0)
        reasoning = (record.context or {}).get("reasoning_tokens", "—")
        print(f"  {name:<4} {model:<15} {record.input_tokens or 0:>9,} "
              f"{record.output_tokens or 0:>8,} {str(reasoning):>10} "
              f"{(record.latency_ms or 0) / 1000:>8.1f}s "
              f"{('$%.4f' % cost) if cost else '—':>9}")

    print(f"\n{RULE}\nDETAIL\n{RULE}")
    for name, model, _, card, _, _ in results:
        print(f"\n  {name} ({model})")
        if card.missing:
            print(f"    missing: {', '.join(card.missing)}")
        if card.spurious:
            print(f"    spurious: {', '.join(card.spurious)}")
        if not card.gate_ok:
            for reason in card.gate_reasons:
                print(f"    gate: {reason}")
        for miss in card.fields.misses[:12]:
            print(f"    {miss['account']} · {miss['field']}: "
                  f"expected `{miss['expected']}`, got `{miss['got']}`")
        for wrong in card.pages_wrong:
            print(f"    {wrong['account']} · pages: expected {wrong['expected']}, "
                  f"got {wrong['got']}")

    async with async_session_maker() as db:
        after = await db.get(CreditReport, args.report)
        banked_after = json.dumps(
            (after.extraction_checkpoint or {}).get("batches") or {}, sort_keys=True
        )
    print(f"\n{RULE}")
    if banked_after == banked_before:
        print("  Banked batches unchanged — this benchmark stored nothing.")
    else:
        print("  WARNING: the report's banked batches changed during this run.")
    print("  No production model has been chosen. These are measurements only.")
    print(RULE)

    if args.as_json:
        payload = {
            "batch": plan.to_dict(),
            "results": [{"config": n, "model": m, "detail": d, **c.to_dict(),
                         "cost": {"input_tokens": r.input_tokens if r else None,
                                  "output_tokens": r.output_tokens if r else None,
                                  "latency_ms": r.latency_ms if r else None,
                                  "estimated_cost_usd": r.estimated_cost_usd if r else None,
                                  "context": (r.context if r else None)}}
                        for n, m, d, c, r, _ in results],
        }
        Path(args.as_json).write_text(json.dumps(payload, indent=2, default=str))
        print(f"\n  written to {args.as_json}")
    return 0


def run(argv: list[str] | None = None) -> int:
    """Entry point shared with the repository's scripts/ wrapper."""
    return asyncio.run(main(argv))


if __name__ == "__main__":
    raise SystemExit(run())
