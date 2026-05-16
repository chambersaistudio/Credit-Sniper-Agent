"""
Tests for the credit report PDF parser.
Uses synthetic credit report text to validate extraction logic.
"""
import pytest
from app.services.pdf_parser import (
    _detect_bureau,
    _extract_credit_score,
    _extract_personal_info,
    _extract_report_date,
    _parse_account_block,
    _extract_inquiries_raw,
    check_7_year_rule,
)

SAMPLE_EQUIFAX_TEXT = """
EQUIFAX Credit Report
Report Date: January 15, 2024
Consumer Name: John Michael Smith
Date of Birth: 01/15/1985
SSN: XXX-XX-1234
Current Address: 123 Main Street, Anytown CA 90210

Credit Score: 620

ACCOUNTS

Account #: Capital One Credit Card
Account Number: XXXX-XXXX-XXXX-4892
Account Type: Revolving
Account Status: Charged Off
Balance: $3,450
Credit Limit: $5,000
Date Opened: 03/2019
Date Closed: 08/2022
Date of First Delinquency: 01/2022
Date Last Reported: 12/2023

Account #: Student Loan Services
Account Number: SL-XXXX-7821
Account Type: Installment
Account Status: Open
Balance: $18,500
Monthly Payment: $215
Date Opened: 08/2018
Date Last Reported: 01/2024

INQUIRIES

Hard Inquiries:
Chase Bank — Date: 03/15/2023
Capital One — Date: 06/22/2022
"""


def test_detect_bureau_equifax():
    assert _detect_bureau(SAMPLE_EQUIFAX_TEXT) == "equifax"


def test_detect_bureau_tri_merge():
    text = "Equifax Experian TransUnion credit report"
    assert _detect_bureau(text) == "tri_merge"


def test_extract_credit_score():
    score = _extract_credit_score(SAMPLE_EQUIFAX_TEXT)
    assert score == 620


def test_extract_credit_score_out_of_range():
    score = _extract_credit_score("Score: 999")
    assert score is None


def test_extract_personal_info():
    info = _extract_personal_info(SAMPLE_EQUIFAX_TEXT)
    assert info.get("ssn_last_four") == "1234"


def test_extract_report_date():
    date = _extract_report_date(SAMPLE_EQUIFAX_TEXT)
    assert date is not None
    assert "2024" in date


def test_parse_account_block_charge_off():
    block = """
    Account #: Capital One Credit Card
    Account Number: XXXX-4892
    Account Status: Charged Off
    Balance: $3,450
    Date of First Delinquency: 01/2022
    Date Last Reported: 12/2023
    """
    account = _parse_account_block(block)
    assert account.get("account_status") == "charged_off"
    assert account.get("balance") == 3450.0


def test_parse_account_block_balance_parsing():
    block = "Account Status: open\nBalance: $12,500.00\nAccount Number: XXXX-1234"
    account = _parse_account_block(block)
    assert account.get("balance") == 12500.0


def test_check_7_year_rule_old_account():
    # Account from 2015 — well past 7 years from 2024
    result = check_7_year_rule("01/2015", "01/2024")
    assert result is True


def test_check_7_year_rule_recent_account():
    # Account from 2020 — within 7 years from 2024
    result = check_7_year_rule("01/2020", "01/2024")
    assert result is False


def test_check_7_year_rule_invalid_date():
    # Should not raise — return False on parse error
    result = check_7_year_rule("not-a-date")
    assert result is False
