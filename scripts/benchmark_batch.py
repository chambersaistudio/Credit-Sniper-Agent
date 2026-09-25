#!/usr/bin/env python3
"""
Benchmark ONE batch across detailed-extraction models.

A thin wrapper around `app.operator.batch_benchmark`, which is where the logic lives
so that it also ships inside the deployed image — the Railway build copies
`backend/` alone, so this `scripts/` directory does not exist in the
container. Inside it, run the same command as:

    python -m app.operator.batch_benchmark --report <id> --batch b0 --truth-inline '<json>'

Run this wrapper with --help for the full options; they are identical.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.operator.batch_benchmark import run  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run())
