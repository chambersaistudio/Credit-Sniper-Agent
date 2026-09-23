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
    _extract_accounts_raw,
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


def test_extract_accounts_raw_keeps_creditor_with_its_account_number():
    # "Creditor:" and "Account Number:" belong to the same record — they
    # must not be split into two separate, incomplete account entries.
    text = """
ACCOUNTS

Creditor: CAPITAL ONE BANK
Account Number: XXXX-XXXX-XXXX-4521
Account Status: Charged Off
Balance: $3,450.00

Creditor: MIDLAND CREDIT MANAGEMENT
Account Number: XXXX-9988
Account Status: Open Collection
Balance: $890.00
"""
    accounts = _extract_accounts_raw(text)
    assert len(accounts) == 2
    names = {a.get("creditor_name") for a in accounts}
    assert names == {"CAPITAL ONE BANK", "MIDLAND CREDIT MANAGEMENT"}
    for account in accounts:
        assert account.get("account_number") is not None
        assert account.get("balance") is not None


def test_extract_accounts_raw_excludes_inquiries_section():
    text = """
ACCOUNTS

Creditor: CAPITAL ONE BANK
Account Number: XXXX-4521
Balance: $3,450.00

INQUIRIES

Creditor: CHASE BANK
Inquiry Date: 01/2024
"""
    accounts = _extract_accounts_raw(text)
    assert len(accounts) == 1
    assert accounts[0]["creditor_name"] == "CAPITAL ONE BANK"


def test_extract_inquiries_raw_matches_bare_inquiries_header():
    text = """
ACCOUNTS

Creditor: CAPITAL ONE BANK
Account Number: XXXX-4521

INQUIRIES

Chase Bank — Date: 03/15/2023
"""
    inquiries = _extract_inquiries_raw(text)
    assert len(inquiries) == 1
    assert "Chase" in inquiries[0]["creditor_name"]


def test_extract_inquiries_raw_single_newline_separated_records():
    # No blank line after the header and no blank line between records —
    # every inquiry line still needs to be extracted, not just the first.
    text = "ACCOUNTS\n\nCreditor: CAPITAL ONE BANK\n\nINQUIRIES\nChase Bank — Date: 01/15/2024\nCapital One — Date: 06/22/2022\n"
    inquiries = _extract_inquiries_raw(text)
    assert len(inquiries) == 2
    names = {i["creditor_name"] for i in inquiries}
    assert any("Chase" in n for n in names)
    assert any("Capital" in n for n in names)
