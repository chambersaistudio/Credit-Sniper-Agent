"""Regression tests for source-equivalent payment-history glyphs.

No network, database, or model calls.
"""
from types import SimpleNamespace

from app.services.benchmark.batch_scoring import score_batch
from app.services.benchmark.groundtruth import normalize_text


def _account(raw_code: str):
    return SimpleNamespace(
        creditor_name="ATLAS",
        account_number="RVBTYRXXXX",
        payment_history=[SimpleNamespace(year=2026, month=1, raw_status_code=raw_code)],
        source_pages=[3],
    )


def _truth(raw_code: str):
    return [{
        "creditor_name": "ATLAS",
        "account_number": "RVBTYRXXXX",
        "payment_history": {"2026-01": raw_code},
        "source_pages": [3],
    }]


def test_experian_current_glyph_aliases_normalize_to_the_same_state():
    assert normalize_text("#") == normalize_text("✓") == normalize_text("✔")


def test_distinct_payment_states_are_not_collapsed():
    assert normalize_text("ND") != normalize_text("-")
    assert normalize_text("CO") != normalize_text("C")
    assert normalize_text("30") != normalize_text("60")


def test_batch_score_treats_hash_and_checkmark_as_the_same_report_state():
    card = score_batch("b0", "A", [_account("#")], _truth("✓"))

    assert card.payment_history.correct == 1
    assert card.payment_history.total == 1
    assert card.payment_history.misses == []


def test_batch_score_still_rejects_materially_different_payment_states():
    card = score_batch("b0", "A", [_account("ND")], _truth("-"))

    assert card.payment_history.correct == 0
    assert card.payment_history.total == 1
    assert card.payment_history.misses[0]["expected"] == "-"
    assert card.payment_history.misses[0]["got"] == "ND"
