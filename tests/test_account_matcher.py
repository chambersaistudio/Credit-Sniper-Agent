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


def test_different_account_numbers_never_match_regardless_of_name():
    a = {"creditor_name": "Capital One", "account_number": "4521", "date_opened": "03/2015"}
    b = {"creditor_name": "Capital One", "account_number": "9988", "date_opened": "03/2015"}
    result = score_match(a, b)
    assert result.confidence == 0.0


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
