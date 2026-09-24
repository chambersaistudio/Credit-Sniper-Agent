"""
Regression tests for the Experian single-bureau parser, the extraction
quality gate, and the "no dispute vs. need more evidence" fix.

Locks in the ground truth of the first live real-report ingestion (a 22-page
Experian report) against a sanitized fixture with the same flattened
two-column pdfplumber layout — colonless labels, and "Account name" (the
tradeline/collector) distinct from "Original creditor" (the debt's origin).
The earlier generic parser produced only six garbage rows named after the
collections' original creditors, with nearly every field blank.
"""
import os
from types import SimpleNamespace

from app.services import experian_parser as E
from app.services.extraction_quality import (
    account_is_substantial, assess_accounts, inquiry_is_suspicious, record_is_disputable,
)
from app.services.pdf_parser import _detect_bureau, _extract_credit_score, _extract_report_date
from app.services.reasoning_engine import ClaimProposalOut, validate_proposal
from app.services.credit_profile import AccountView

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "experian_sample.txt")


def _text() -> str:
    with open(FIXTURE) as f:
        return f.read()


def _by_name(accounts):
    # 14 distinct names but NAVY FEDERAL appears twice (card + secured loan);
    # key the lookup by (name, type) so both are addressable.
    return {(a["creditor_name"], a.get("account_type")): a for a in accounts}


def test_report_header_score_and_date():
    text = _text()
    assert _detect_bureau(text) == "experian"
    assert _extract_credit_score(text) == 580
    assert _extract_report_date(text) == "Sep 24, 2026"


def test_extracts_all_fifteen_tradelines_with_creditor_names():
    accounts = E.parse_experian_accounts(_text())
    assert len(accounts) == 15
    names = sorted(a["creditor_name"] for a in accounts)
    assert names == sorted([
        "ATLAS", "CAPITAL ONE", "CREDIT ACCEPTANCE CORP", "EXTRA", "MISSION LANE TAB BANK",
        "NAVY FEDERAL CR UNION", "NAVY FEDERAL CR UNION", "SBNASELFLNDR", "SELF FINANCIAL/LEAD BA",
        "CAINE & WEINER", "CREDENCE RESOURCE MANA", "CREDIT COLLECTION SERV",
        "JEFFERSON CAPITAL SYST", "JEFFERSON CAPITAL SYST", "LVNV FUNDING LLC",
    ])
    # Every tradeline is a complete-looking record, not a bare name.
    assert all(account_is_substantial(a) for a in accounts)


def test_collections_keep_collector_and_original_creditor_distinct():
    accounts = _by_name(E.parse_experian_accounts(_text()))
    # The tradeline identity is the collector; the original creditor is separate.
    caine = accounts[("CAINE & WEINER", "Collection")]
    assert caine["original_creditor"] == "PROGRESSIVE"
    assert caine["creditor_name"] == "CAINE & WEINER"

    expected_original = {
        "CAINE & WEINER": "PROGRESSIVE",
        "CREDENCE RESOURCE MANA": "AT T",
        "CREDIT COLLECTION SERV": "PROGRESSIVE",
        "LVNV FUNDING LLC": "CAPITAL BANK OPEN SKY",
    }
    for name, original in expected_original.items():
        assert accounts[(name, "Collection")]["original_creditor"] == original
    jefferson = [a for a in E.parse_experian_accounts(_text()) if a["creditor_name"] == "JEFFERSON CAPITAL SYST"]
    assert {a["original_creditor"] for a in jefferson} == {"MISSION LANE CREDIT CARD", "T-MOBILE"}

    # No regular tradeline invents an original creditor from the "-" placeholder.
    atlas = accounts[("ATLAS", "Line of Credit")]
    assert atlas.get("original_creditor") is None


def test_regular_tradeline_all_fields():
    atlas = _by_name(E.parse_experian_accounts(_text()))[("ATLAS", "Line of Credit")]
    assert atlas["account_number"] == "5299XXXXXXXX1234"
    assert atlas["account_type"] == "Line of Credit"
    assert atlas["account_status"] == "open" and atlas["account_status_raw"] == "Open/Never late"
    assert atlas["balance"] == 16.0
    assert atlas["credit_limit"] == 1000.0
    assert atlas["high_balance"] == 30.0
    assert atlas["monthly_payment"] == 16.0
    assert atlas["date_opened"] == "Dec 22, 2025"
    assert atlas["date_last_reported"] == "Jun 25, 2026"
    assert atlas.get("past_due_amount") is None  # printed as "-"


def test_collection_tradeline_all_fields():
    caine = _by_name(E.parse_experian_accounts(_text()))[("CAINE & WEINER", "Collection")]
    assert caine["account_number"] == "88XXXX2211"
    assert caine["account_status"] == "collection" and caine["account_status_raw"] == "Collection account"
    assert caine["balance"] == 1204.0
    assert caine["past_due_amount"] == 1204.0
    assert caine["high_balance"] == 1204.0
    assert caine["original_amount"] == 1204.0
    assert caine["date_opened"] == "Feb 15, 2026"
    assert caine["date_last_reported"] == "May 10, 2026"
    assert caine["remarks"] == "Placed for collection"


def test_two_inquiries_parsed_and_debris_dropped():
    inquiries = E.parse_experian_inquiries(_text())
    assert inquiries == [
        {"creditor_name": "CAPITAL ONE", "inquiry_date": "Sep 23, 2026", "inquiry_type": "hard"},
        {"creditor_name": "CREDIT ONE BANK, NATIO", "inquiry_date": "May 15, 2026", "inquiry_type": "hard"},
    ]


def test_quality_gate_accepts_the_real_parse():
    quality = assess_accounts(E.parse_experian_accounts(_text()))
    assert quality.complete and quality.total == 15 and quality.substantial == 15


def test_quality_gate_rejects_the_old_garbage_parse():
    # What the old parser produced: six rows named after the collections'
    # original creditors, every reportable field blank.
    garbage = [{"creditor_name": n} for n in
               ["PROGRESSIVE", "AT T", "PROGRESSIVE", "MISSION LANE CREDIT CARD", "T-MOBILE", "CAPITAL BANK OPEN SKY"]]
    quality = assess_accounts(garbage)
    assert not quality.complete
    assert quality.substantial == 0
    assert quality.reasons  # explains why it was rejected


def test_suspicious_inquiries_are_flagged():
    assert inquiry_is_suspicious({"creditor_name": "(800) 2"})
    assert inquiry_is_suspicious({"creditor_name": "Prepared For CORNELIUS CHAMBERS"})
    assert inquiry_is_suspicious({"creditor_name": ""})
    assert not inquiry_is_suspicious({"creditor_name": "CAPITAL ONE"})


def test_record_disputability_precondition():
    # A row that is only a name (a parse failure) is not evaluable...
    assert not record_is_disputable({"creditor_name": "PROGRESSIVE"})
    # ...but a name plus any real reportable field is.
    assert record_is_disputable({"creditor_name": "CAINE & WEINER", "balance": 1204.0})
    assert not record_is_disputable({"balance": 1204.0})  # no name


def _view():
    return AccountView(
        canonical=SimpleNamespace(creditor_name="X", account_type="collection"),
        records=[{"bureau": "experian"}], history_count=1, findings=[],
    )


def _proposal(**kw):
    base = dict(
        has_dispute_ground=False, reasoning="…", supporting_finding_ids=[], disputed_fields=[],
        recipients=[], legal_basis=[], requested_remedy=None, additional_evidence_needed=[],
        recommended_action="no_dispute", confidence=0.9,
    )
    base.update(kw)
    return ClaimProposalOut(**base)


def test_no_dispute_with_evidence_request_becomes_need_more_evidence():
    # The PROGRESSIVE bug: model admits it lacks details and asks for more,
    # yet returns no_dispute. That contradiction must resolve to need-more-evidence.
    out, _, notes = validate_proposal(
        _proposal(additional_evidence_needed=["the account's balance and status"]), _view(), {})
    assert out.recommended_action == "need_more_evidence"
    assert any("more evidence" in n for n in notes)


def test_clean_no_dispute_is_preserved():
    out, _, _ = validate_proposal(_proposal(), _view(), {})
    assert out.recommended_action == "no_dispute"
