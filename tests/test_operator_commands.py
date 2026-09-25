"""
The operator commands, and where they live.

Two things are asserted here that nothing else covers.

**They ship inside the image.** The Railway build copies `backend/` alone, so
the repository's `scripts/` directory does not exist in the container. The
logic therefore lives under `app/operator/`, invocable as
`python -m app.operator.<command>`, and `scripts/*.py` are wrappers around
exactly those modules — one implementation, two entry points.

**They cannot change production behaviour.** Nothing in the serving path
imports `app.operator`, so its presence in the image is inert.

Also pins the page-send arithmetic to the REAL banked Experian index. An
earlier write-up quoted 19 page-sends from a mock index whose tradelines sat
on pages 3–10; the real one spans 3–17 and costs 23. A number that appears in
the documentation should fail a test when it drifts, not a reader.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.document_extraction.batching import plan_batches, plan_summary
from app.services.document_extraction.index_schema import IndexedTradeline, ReportIndex

BACKEND = Path(__file__).resolve().parent.parent / "backend"
REPO = Path(__file__).resolve().parent.parent
COMMANDS = ("index_pass", "extract_batch", "batch_benchmark")


# ── The real Experian plan ──────────────────────────────────────────────
# 15 tradelines across pages 3–17 of a 28-page report, as the production
# index actually banked them. The page count is the banked index's own
# total_pages, confirmed against a deterministic pypdf count of the stored
# original.
PRODUCTION_PAGES = [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]


def production_index() -> ReportIndex:
    return ReportIndex(
        bureau="experian", document_created_date="Sep 24, 2026", report_date=None,
        score=580, score_type="FICO Score 8", tradeline_count=15,
        tradelines=[
            IndexedTradeline(creditor_name=f"TRADELINE {i}", original_creditor=None,
                             account_number=f"{1000 + i}", account_type="Credit card",
                             source_pages=[page], heading_excerpt=None)
            for i, page in enumerate(PRODUCTION_PAGES, start=1)
        ],
        total_pages=28, unreadable_pages=[],
    )


def test_the_real_experian_plan_matches_what_production_banked():
    """The exact plan an operator verified against the live report."""
    plans = plan_batches(production_index())
    expected = [
        ("b0", [3, 4, 5, 6], [2, 3, 4, 5, 6, 7], [2, 7]),
        ("b1", [7, 8, 9, 10], [6, 7, 8, 9, 10, 11], [6, 11]),
        ("b2", [11, 12, 13, 14], [10, 11, 12, 13, 14, 15], [10, 15]),
        ("b3", [15, 16, 17], [14, 15, 16, 17, 18], [14, 18]),
    ]
    assert len(plans) == len(expected)
    for plan, (batch_id, indexed, bundle, padding) in zip(plans, expected):
        assert plan.batch_id == batch_id
        assert sorted({p for t in plan.tradelines for p in t.source_pages}) == indexed
        assert list(plan.pages) == bundle
        assert list(plan.padding) == padding


def test_the_documented_page_send_saving_is_what_the_code_produces():
    """23 page-sends against 112, a 79.5% reduction — the figures in
    docs/extraction-scaling.md."""
    index = production_index()
    plans = plan_batches(index)
    summary = plan_summary(plans, index)

    assert [len(p.pages) for p in plans] == [6, 6, 6, 5]
    assert summary["pages_sent"] == 23
    assert summary["pages_if_whole_document"] == 28 * 4 == 112
    assert summary["pages_saved"] == 89
    reduction = summary["pages_saved"] / summary["pages_if_whole_document"]
    assert round(reduction * 100, 1) == 79.5


def test_the_documentation_quotes_those_same_numbers():
    """Cheap, and it would have caught both earlier stale sets: 19/85% from a
    mock index, then 124/81.5% from a wrong page count."""
    doc = (REPO / "docs" / "extraction-scaling.md").read_text()
    assert "23 page-sends" in doc
    assert "79.5%" in doc
    assert "112 page-sends" in doc
    for stale in ("19 page-sends", "85% less", "124 page-sends", "81.5%"):
        assert stale not in doc, f"the stale figure {stale!r} is back"


# ── Where the commands live ─────────────────────────────────────────────

@pytest.mark.parametrize("command", COMMANDS)
def test_each_operator_command_runs_from_inside_the_image_layout(command):
    """`python -m app.operator.<command>` with backend/ as the working
    directory — exactly what /app looks like in the container."""
    result = subprocess.run(
        [sys.executable, "-m", f"app.operator.{command}", "--help"],
        cwd=BACKEND, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    # The help text must name the form that actually works in the container.
    assert f"app.operator.{command}" in result.stdout


@pytest.mark.parametrize("command,module", [
    ("index_pass", "index_pass"),
    ("extract_batch", "extract_batch"),
    ("benchmark_batch", "batch_benchmark"),
])
def test_the_repo_script_is_a_wrapper_around_the_same_implementation(command, module):
    """One implementation, two entry points — a command cannot behave
    differently depending on where it was started."""
    source = (REPO / "scripts" / f"{command}.py").read_text()
    assert f"from app.operator.{module} import run" in source
    assert f"app.operator.{module}" in source
    # The wrapper holds no logic of its own to drift.
    assert len(source.splitlines()) < 30


def test_the_serving_path_never_imports_the_operator_package():
    """The guarantee that shipping these commands cannot change production
    behaviour. Run in a subprocess so another test's imports cannot mask it."""
    probe = (
        "import sys; import app.main; "
        "leaked = sorted(m for m in sys.modules if m.startswith('app.operator')); "
        "print(leaked)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=BACKEND, capture_output=True, text=True, timeout=120,
        env={**__import__("os").environ, "DB_NULL_POOL": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("[]"), f"app.main pulled in {result.stdout.strip()}"


def test_the_benchmark_defaults_to_the_first_batch_only():
    """Only b0 runs unless an operator names another, and there is no flag
    that sweeps them all."""
    from app.operator import batch_benchmark

    source = Path(batch_benchmark.__file__).read_text()
    assert '"--batch", default="b0"' in source
    for sweep in ("--all", "for plan in plans:", "for batch_id in"):
        assert sweep not in source, f"{sweep!r} would run more than one batch"


def test_the_benchmark_cannot_bank_a_batch():
    """It reads through extract_batch, which touches no database, and never
    through run_report_batch, which is what banks."""
    from app.operator import batch_benchmark

    source = Path(batch_benchmark.__file__).read_text()
    assert "extract_batch" in source
    assert "run_report_batch" not in source, "the benchmark must not bank"
    # And it proves it rather than only promising it.
    assert "banked_before" in source and "banked_after" in source
