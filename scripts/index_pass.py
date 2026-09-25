#!/usr/bin/env python3
"""
Stage 1 validation: run the INDEX PASS ONLY against a real report.

A thin wrapper around `app.operator.index_pass`, which is where the logic lives
so that it also ships inside the deployed image — the Railway build copies
`backend/` alone, so this `scripts/` directory does not exist in the
container. Inside it, run the same command as:

    python -m app.operator.index_pass --report <id> --expect 15

Run this wrapper with --help for the full options; they are identical.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.operator.index_pass import run  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run())
