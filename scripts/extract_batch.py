#!/usr/bin/env python3
"""
Stage 2: run ONE batch of detailed tradeline extraction.

A thin wrapper around `app.operator.extract_batch`, which is where the logic lives
so that it also ships inside the deployed image — the Railway build copies
`backend/` alone, so this `scripts/` directory does not exist in the
container. Inside it, run the same command as:

    python -m app.operator.extract_batch --report <id> --batch b0

Run this wrapper with --help for the full options; they are identical.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.operator.extract_batch import run  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run())
