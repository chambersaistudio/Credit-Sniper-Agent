#!/usr/bin/env python3
"""
Benchmark the document-extraction pipeline across model configurations.

    python scripts/benchmark_extraction.py golden/*.json --out results/

Each argument is a ground-truth JSON file describing one report and pointing
at its PDF (see backend/app/services/benchmark/groundtruth.py, and
tests/fixtures/benchmark/ for a worked example). Configurations:

    A  gpt-5.6-luna  extract / gpt-5.6-terra audit / detail=low
    B  gpt-5.6-terra extract / gpt-5.6-terra audit / detail=low
    C  gpt-5.6-sol   extract / gpt-5.6-sol   audit / detail=high   (baseline)

Sol is the baseline and the escalation target, not a candidate default: the
report also simulates "run A (or B), escalate to Sol only when A and B
disagree or the quality gate refuses the reading", which is the number that
should decide anything.

This spends real provider money — two passes per document per configuration —
and reads real credit-report PDFs, so run it deliberately, on a machine
allowed to reach the provider, and keep the PDFs and ground truth out of git.

It changes no production defaults: configurations are applied for the duration
of a run and restored, and usage records go to the benchmark rather than to
the application's usage table.
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.services.benchmark import (  # noqa: E402
    CONFIGS, config_by_name, load_ground_truth, run_suite,
)
from app.services.benchmark.report import render_markdown  # noqa: E402


def _load(paths: list[str]):
    loaded = []
    for path in paths:
        truth = load_ground_truth(path)
        if truth.pdf_path is None or not truth.pdf_path.exists():
            raise SystemExit(f"{path}: 'pdf' must point at the report PDF (looked for {truth.pdf_path})")
        document = truth.pdf_path.read_bytes()
        if not document.startswith(b"%PDF-"):
            raise SystemExit(f"{truth.pdf_path}: not a PDF")
        loaded.append((truth, document))
    return loaded


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("truth", nargs="+", help="ground-truth JSON files, one per report")
    parser.add_argument("--config", action="append", metavar="NAME",
                        help="limit to these configs (default: all of A, B, C)")
    parser.add_argument("--out", default="benchmark-results", help="output directory")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would run, and spend nothing")
    args = parser.parse_args()

    configs = tuple(config_by_name(name) for name in args.config) if args.config else CONFIGS
    truths = _load(args.truth)

    print(f"{len(truths)} document(s) × {len(configs)} config(s) = "
          f"{len(truths) * len(configs) * 2} model calls")
    for truth, document in truths:
        print(f"  · {truth.name}: {truth.account_count} accounts, "
              f"{len(truth.inquiries)} inquiries, {len(document) / 1024:.0f} KB")
    for config in configs:
        print(f"  · {config.label}")
    if args.dry_run:
        print("\ndry run: nothing was sent to a provider")
        return 0

    if not os.getenv("OPENAI_API_KEY"):
        print("\nOPENAI_API_KEY is not set — the document tiers have no credential", file=sys.stderr)
        return 2

    result = await run_suite(truths, configs)
    result["generated_at"] = datetime.now(timezone.utc).isoformat()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    (out / f"benchmark-{stamp}.json").write_text(json.dumps(result, indent=2, default=str))
    markdown = render_markdown(result)
    (out / f"benchmark-{stamp}.md").write_text(markdown)

    print()
    print(markdown)
    print(f"written to {out}/benchmark-{stamp}.{{json,md}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
