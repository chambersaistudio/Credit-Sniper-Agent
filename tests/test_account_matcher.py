import uuid

from app.services.account_matcher import (
    Candidate,
    LinkedRecord,
    choose_candidate,
    normalize_creditor_name,
    score_match,
    AUTO_LINK_THRESHOLD,
)


def test_normalize_creditor_name_strips_suffixes_and_punctuation():
    assert normalize_creditor_name("Capital One Bank, N.A.") == "capital one"
    assert normalize_creditor_name("MIDLAND CREDIT MANAGEMENT LLC") == "midland credit management"


def test_normalize_creditor_name_handles_none():
    assert normalize_creditor_name(None) == ""


def test_same_account_high_confidence_match():
    a = {"creditor_name": "Capital One Bank", "account_number": "XXXX-XXXX-XXXX-4521", "date_opened": "03/15/2015"}
    b = {"creditor_name": "CAPITAL ONE", "account_number": "4521", "date_opened": "03/2015"}
    result = score_match(a, b)
    assert result.confidence >= AUTO_LINK_THRESHOLD
    assert result.matched_fields["account_number_suffix"] == 1.0


def test_different_account_numbers_do_not_auto_merge():
    """A masked number can be scrambled between bureaus, so a mismatch is
    strong evidence against rather than an outright veto — but it must never
    auto-merge on the strength of the name and date alone."""
    a = {"creditor_name": "Capital One", "account_number": "4521", "date_opened": "03/2015"}
    b = {"creditor_name": "Capital One", "account_number": "9988", "date_opened": "03/2015"}
    result = score_match(a, b)
    assert not result.auto
    assert result.matched_fields["account_number_suffix"] == 0.0


def test_different_creditors_score_low():
    a = {"creditor_name": "Capital One", "account_number": None, "date_opened": "03/2015"}
    b = {"creditor_name": "Midland Credit Management", "account_number": None, "date_opened": "01/2022"}
    result = score_match(a, b)
    assert result.confidence < AUTO_LINK_THRESHOLD


def test_missing_fields_produce_conservative_moderate_confidence():
    # Same name, but no account number on either side and open dates a
    # year apart — should not clear the auto-link bar on name alone.
    a = {"creditor_name": "Chase Bank", "account_number": None, "date_opened": "01/2020"}
    b = {"creditor_name": "Chase", "account_number": None, "date_opened": "01/2021"}
    result = score_match(a, b)
    assert result.confidence < AUTO_LINK_THRESHOLD


REPORT_A, REPORT_B = uuid.uuid4(), uuid.uuid4()
CAP_ONE = {"creditor_name": "Capital One", "account_number": "XXXX-4521", "date_opened": "03/2015"}


def _candidate(*records):
    return Candidate(canonical_id=uuid.uuid4(), records=[LinkedRecord(*r) for r in records])


def test_links_across_bureaus():
    candidate = _candidate(("equifax", REPORT_A, CAP_ONE))
    chosen, _ = choose_candidate(CAP_ONE, "experian", REPORT_B, [candidate])
    assert chosen is candidate


def test_never_merges_two_tradelines_from_the_same_bureau_report():
    # Previously a canonical account already holding an Experian record could
    # absorb a second, identical-looking Experian tradeline from the same report.
    candidate = _candidate(("equifax", REPORT_A, CAP_ONE), ("experian", REPORT_B, CAP_ONE))
    chosen, _ = choose_candidate(CAP_ONE, "experian", REPORT_B, [candidate])
    assert chosen is None


def test_newer_report_from_same_bureau_links_to_existing_account():
    candidate = _candidate(("experian", REPORT_A, CAP_ONE))
    chosen, _ = choose_candidate(CAP_ONE, "experian", REPORT_B, [candidate])
    assert chosen is candidate


def test_scores_against_every_linked_record_not_just_the_first():
    weak = {"creditor_name": "Cap One Services", "account_number": None, "date_opened": None}
    candidate = _candidate(("equifax", REPORT_A, weak), ("transunion", REPORT_A, CAP_ONE))
    chosen, result = choose_candidate(CAP_ONE, "experian", REPORT_B, [candidate])
    assert chosen is candidate
    assert result.confidence >= 0.9


# ── Cross-bureau matching (Experian + TransUnion live disclosures) ─────────

EXPERIAN_CAP_ONE = {
    "creditor_name": "CAPITAL ONE", "account_number": "517805XXXXXX8842", "account_type": "Credit card",
    "date_opened": "Mar 3, 2024", "credit_limit": 1500.0, "high_balance": 1200.0,
    "payment_history": [{"year": 2026, "month": m, "raw_status_code": "OK"} for m in range(1, 7)],
}


def test_same_account_across_bureaus_merges():
    """The whole point: uploading a second bureau must not double the profile."""
    transunion = {
        "creditor_name": "CAPITAL ONE BANK USA NA", "account_number": "517805XXXXXX8842",
        "account_type": "Credit Card", "date_opened": "03/03/2024", "credit_limit": 1500.0,
        "high_balance": 1200.0,
        "payment_history": [{"year": 2026, "month": m, "raw_status_code": "OK"} for m in range(1, 7)],
    }
    result = score_match(EXPERIAN_CAP_ONE, transunion)
    assert result.auto, result.matched_fields


def test_scrambled_account_number_still_reaches_review():
    """TransUnion warns masked digits may be scrambled. Everything else
    agreeing should land in review — not auto-merge, not silently separate."""
    scrambled = {**EXPERIAN_CAP_ONE, "account_number": "517805XXXXXX1234"}
    result = score_match(EXPERIAN_CAP_ONE, scrambled)
    assert result.needs_review, result.confidence
    assert not result.auto


def test_name_alone_is_never_enough():
    a = {"creditor_name": "CAPITAL ONE"}
    b = {"creditor_name": "CAPITAL ONE"}
    result = score_match(a, b)
    assert not result.auto  # coverage penalty keeps a lone name below the bar


def test_collection_never_merges_with_the_original_tradeline():
    """Jefferson Capital's collection and the Mission Lane tradeline it came
    from are related, not the same entry. Merging them would erase the
    relationship the consumer needs to dispute each side separately."""
    collection = {
        "creditor_name": "JEFFERSON CAPITAL SYST", "original_creditor": "MISSION LANE CREDIT CARD",
        "account_type": "Collection", "account_status": "collection", "date_opened": "Dec 5, 2025",
    }
    original = {
        "creditor_name": "MISSION LANE CREDIT CARD", "account_type": "Credit card",
        "account_status": "charged_off", "date_opened": "Dec 5, 2025",
    }
    result = score_match(collection, original)
    assert result.confidence == 0.0
    assert result.blocked_by == "collection_vs_original_tradeline"


def test_same_debt_buyer_different_debts_never_merge():
    """Two Jefferson Capital collections for different original creditors are
    different debts, however alike the collector's name is."""
    mission_lane = {
        "creditor_name": "JEFFERSON CAPITAL SYST", "original_creditor": "MISSION LANE CREDIT CARD",
        "account_type": "Collection", "account_status": "collection", "date_opened": "Dec 5, 2025",
    }
    t_mobile = {
        "creditor_name": "JEFFERSON CAPITAL SYST", "original_creditor": "T-MOBILE",
        "account_type": "Collection", "account_status": "collection", "date_opened": "Dec 5, 2025",
    }
    result = score_match(mission_lane, t_mobile)
    assert result.confidence == 0.0
    assert result.blocked_by == "different_original_creditor"


def test_same_collection_across_bureaus_does_merge():
    experian = {
        "creditor_name": "CAINE & WEINER", "original_creditor": "PROGRESSIVE", "account_type": "Collection",
        "account_status": "collection", "account_number": "88XXXX2211", "date_opened": "Feb 15, 2026",
        "original_amount": 1204.0,
    }
    transunion = {
        "creditor_name": "CAINE AND WEINER CO", "original_creditor": "PROGRESSIVE INSURANCE",
        "account_type": "Collection", "account_status": "collection", "account_number": "88XXXX2211",
        "date_opened": "02/15/2026", "original_amount": 1204.0,
    }
    assert score_match(experian, transunion).auto


def test_payment_fingerprint_supports_a_match():
    shared = [{"year": 2025, "month": m, "raw_status_code": code}
              for m, code in enumerate(["OK", "OK", "30", "60", "90", "CO"], start=1)]
    a = {"creditor_name": "ATLAS", "account_type": "Line of Credit", "payment_history": shared}
    b = {"creditor_name": "ATLAS", "account_type": "Line of Credit", "payment_history": list(shared)}
    result = score_match(a, b)
    assert result.matched_fields["payment_fingerprint"] == 1.0


def test_conflicting_payment_fingerprint_weakens_a_match():
    a = {"creditor_name": "ATLAS", "account_type": "Line of Credit",
         "payment_history": [{"year": 2025, "month": m, "raw_status_code": "OK"} for m in range(1, 7)]}
    b = {"creditor_name": "ATLAS", "account_type": "Line of Credit",
         "payment_history": [{"year": 2025, "month": m, "raw_status_code": "CO"} for m in range(1, 7)]}
    assert score_match(a, b).matched_fields["payment_fingerprint"] == 0.0
