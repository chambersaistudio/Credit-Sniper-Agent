"""
Privacy guarantee for text sent to AI providers: representative consumer PII
never reaches the provider, while the tradeline data extraction needs does.
"""
from types import SimpleNamespace

import pytest

from app.services.ai_extraction import ExtractedReport, extract_with_ai
from app.services.pdf_parser import _extract_personal_info
from app.services.reasoning_engine import ClaimProposalOut, evaluate_account
from app.services.redaction import Identity, redact

REPORT = """TRANSUNION PERSONAL CREDIT REPORT
Report for: JOHN MICHAEL SMITH
Date of Birth: 01/15/1985
Social Security Number: 123-45-6789
Also Known As: JOHNNY M SMITH
Current Address: 4521 Oak Hollow Drive Apt 12, Springfield, IL 62704
Previous Addresses:
77 W Elm Street
Chicago, IL 60601
Phone: (217) 555-0142
Mobile: 217.555.0199
Email: john.smith85@example.com
Employer: Acme Logistics Inc

SMITH, JOHN M - Page 1 of 4
File masked SSN XXX-XX-6789 on record.
Consumer born January 15, 1985.

Tradelines
DISCOVER BANK   6011********7788   revolving   opened 04/2019   bal 1,250   pays as agreed
CAPITAL ONE BANK USA NA   XXXX-XXXX-XXXX-4521   credit card   charge off   bal $3,450.00   DOFD 06/2016
Date Opened: 03/15/2015
Date Last Reported: 08/2024
Payment History: OK OK 30 60 90 120 CO CO
Remarks: ACCOUNT CLOSED BY CREDIT GRANTOR; CONSUMER DISPUTES - REINVESTIGATION IN PROGRESS
Creditor Name: MIDLAND CREDIT MANAGEMENT
Original Creditor: COMENITY BANK
Account Number: XXXX9988
Balance: $890.00
Past Due: $890.00

Inquiries
CHASE BANK USA 01/15/2024
"""

PII = [
    "JOHN MICHAEL SMITH", "John Michael Smith", "SMITH, JOHN M", "JOHNNY M SMITH",
    "01/15/1985", "January 15, 1985",
    "123-45-6789", "XXX-XX-6789",
    "4521 Oak Hollow Drive", "Springfield", "62704", "77 W Elm Street", "Chicago", "60601",
    "(217) 555-0142", "555-0142", "217.555.0199",
    "john.smith85@example.com", "Acme Logistics",
]

TRADELINE_DATA = [
    "TRANSUNION", "DISCOVER BANK", "6011********7788", "revolving", "04/2019", "1,250", "pays as agreed",
    "CAPITAL ONE BANK USA NA", "XXXX-XXXX-XXXX-4521", "charge off", "$3,450.00", "06/2016",
    "03/15/2015", "08/2024", "OK OK 30 60 90 120 CO CO", "ACCOUNT CLOSED BY CREDIT GRANTOR",
    "CONSUMER DISPUTES - REINVESTIGATION IN PROGRESS", "Creditor Name: MIDLAND CREDIT MANAGEMENT",
    "Original Creditor: COMENITY BANK", "XXXX9988", "$890.00", "Past Due", "CHASE BANK USA", "01/15/2024",
]

USER = SimpleNamespace(
    full_name="John Michael Smith", address="4521 Oak Hollow Drive Apt 12", date_of_birth="01/15/1985",
    phone="217-555-0142", email="john.smith85@example.com",
)


def _identity():
    return Identity.from_sources(_extract_personal_info(REPORT), USER)


def test_header_name_is_detected_in_capitals():
    assert _extract_personal_info(REPORT)["name"] == "JOHN MICHAEL SMITH"


@pytest.mark.parametrize("identity", [None, "known"], ids=["patterns-only", "with-known-identity"])
def test_pii_removed_and_tradelines_kept(identity):
    result = redact(REPORT, _identity() if identity else None)
    lowered = result.text.lower()
    missing_pii = [p for p in PII if p.lower() in lowered]
    if identity is None:
        # Without knowing the consumer, the unlabeled page-footer name and the
        # bare "born <date>" sentence can't be recognized — that's what the
        # known-identity layer is for. Everything else must still go.
        assert set(missing_pii) <= {"SMITH, JOHN M", "January 15, 1985"}
    else:
        assert missing_pii == []
    assert [t for t in TRADELINE_DATA if t not in result.text] == []
    assert result.counts


def test_creditor_name_label_is_not_treated_as_a_consumer_name():
    text = "Creditor Name: MIDLAND CREDIT MANAGEMENT\nAccount Name: DISCOVER\nName: JANE DOE\n"
    out = redact(text).text
    assert "MIDLAND CREDIT MANAGEMENT" in out and "DISCOVER" in out and "JANE DOE" not in out


def test_masked_account_numbers_and_dates_survive():
    text = "Account Number: XXXX-XXXX-XXXX-4521\nOpened 03/15/2015 bal $1,200.00 limit 5,000\nAcct 412345******7788"
    assert redact(text).text == text


def test_address_block_does_not_swallow_the_next_labeled_field():
    text = "Current Address\n1 Main Street\nCreditor: CAPITAL ONE\nBalance: $10.00\n"
    out = redact(text).text
    assert "1 Main Street" not in out and "Creditor: CAPITAL ONE" in out and "Balance: $10.00" in out


async def test_ai_extraction_request_contains_no_pii(fake_ai):
    provider = fake_ai(lambda *_: ExtractedReport(accounts=[], inquiries=[]))
    result = await extract_with_ai(REPORT, _identity(), context={})
    sent = provider.calls[0]["prompt"]
    assert [p for p in PII if p.lower() in sent.lower()] == []
    assert [t for t in TRADELINE_DATA if t not in sent] == []
    assert result.redactions


async def test_redaction_placeholders_are_never_stored_as_data(fake_ai):
    from app.services.ai_extraction import ExtractedAccount

    fields = dict(account_number=None, account_type=None, account_status=None, payment_status=None, balance=None,
                  past_due_amount=None, credit_limit=None, high_balance=None, date_opened=None, date_closed=None,
                  date_of_first_delinquency=None, date_last_reported=None, date_last_payment=None, remarks=None)
    fake_ai(lambda *_: ExtractedReport(
        accounts=[ExtractedAccount(source_excerpt="[REDACTED IDENTITY]", creditor_name="[REDACTED IDENTITY]", **fields),
                  ExtractedAccount(source_excerpt="DISCOVER BANK", creditor_name="DISCOVER BANK", **fields)],
        inquiries=[],
    ))
    result = await extract_with_ai(REPORT, _identity(), context={})
    assert [a["creditor_name"] for a in result.accounts] == ["DISCOVER BANK"]


async def test_reasoning_prompt_redacts_identity_in_free_text(fake_ai):
    from app.services.credit_profile import AccountView

    provider = fake_ai(lambda *_: ClaimProposalOut(
        has_dispute_ground=False, reasoning="x", supporting_finding_ids=[], disputed_fields=[], recipients=[],
        legal_basis=[], requested_remedy=None, additional_evidence_needed=[], recommended_action="no_dispute",
        confidence=0.9,
    ))
    canonical = SimpleNamespace(id=None, user_id=None, creditor_name="CAPITAL ONE", account_type="Credit Card")
    records = [{"bureau": "equifax", "balance": 3450.0, "account_number": "XXXX-4521",
                "remarks": "Dispute filed by John Michael Smith, 217-555-0142, SSN 123-45-6789"}]
    await evaluate_account(AccountView(canonical, records, 1, []), identity=_identity())
    sent = provider.calls[0]["prompt"]
    assert "John Michael Smith" not in sent and "555-0142" not in sent and "123-45-6789" not in sent
    assert "XXXX-4521" in sent and "3450.0" in sent and "CAPITAL ONE" in sent
