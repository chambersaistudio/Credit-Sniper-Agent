from app.services.account_matcher import (
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
