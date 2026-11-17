"""Deterministic Stage-3 merge tests. No network/provider calls."""
import pytest

from app.services.batch_job import plan_fingerprint
from app.services.document_extraction.batch_schema import TradelineBatch
from app.services.document_extraction.batching import plan_batches
from app.services.document_extraction.index_schema import (
    IndexedContact, IndexedInquiry, IndexedSummaryMetric, IndexedTradeline, ReportIndex,
)
from app.services.document_extraction.scaled_merge import (
    ScaledMergeError, merge_scaled_checkpoint,
)
from app.services.document_extraction.schema import ExtractedTradeline


def _index():
    return ReportIndex(
        bureau="experian", document_created_date="Sep 24, 2026", report_date=None,
        score=580, score_type="FICO Score 8", consumer_on_file_since=None,
        summary_metrics=[IndexedSummaryMetric(name="Open accounts", value="1")],
        inquiries=[IndexedInquiry(
            creditor_name="TEST BANK", inquiry_date="Sep 23, 2026",
            inquiry_type=None, inquiry_category="credit_application",
            business_type="Bank Credit Cards",
            contact=IndexedContact(
                name="TEST BANK", address="1 MAIN ST; TESTVILLE, IA 50000", phone="(800) 555-0100"
            ),
            source_pages=[19],
        )],
        public_records=[], tradeline_count=2, total_pages=20, unreadable_pages=[],
        tradelines=[
            IndexedTradeline(
                creditor_name="ALPHA", original_creditor=None, account_number="1111XX",
                account_type="Credit card", source_pages=[3], heading_excerpt="ALPHA",
            ),
            IndexedTradeline(
                creditor_name="BETA", original_creditor=None, account_number="2222XX",
                account_type="Credit card", source_pages=[4], heading_excerpt="BETA",
            ),
        ],
    )


def _account(name, number, page):
    return ExtractedTradeline(
        creditor_name=name, original_creditor=None, sold_to=None, account_number=number,
        account_type="Credit card", open_closed="Open", status_raw="Open/Never late.",
        status_normalized=None, payment_status=None, report_classification=None,
        balance="$0", past_due_amount=None, high_balance=None, credit_limit="$500",
        original_amount=None, monthly_payment=None, terms=None, responsibility="Individual",
        consumer_dispute=None, date_opened="Jan 01, 2024", date_closed=None,
        date_first_delinquency=None, date_last_reported=None, date_last_payment=None,
        status_updated=None, balance_updated=None, identity_evidence=name,
        remarks=None, contact=None, payment_history=[], source_pages=[page],
        field_evidence=[],
    )


def _checkpoint():
    index = _index()
    plans = plan_batches(index)
    batches = {}
    for plan in plans:
        batch = TradelineBatch(accounts=[_account(t.creditor_name, t.account_number, t.source_pages[0])
                                         for t in plan.tradelines], missing_tradelines=[], unreadable_pages=[])
        batches[plan.batch_id] = {
            "batch": batch.model_dump(mode="json"),
            "model": "gpt-5.6-luna",
            "quality": {"ok": True},
            "plan": plan.to_dict(),
            "plan_fingerprint": plan_fingerprint(plan),
        }
    return {"index": index.model_dump(mode="json"), "batches": batches}


def test_stage3_merge_builds_the_existing_full_extraction_shape():
    merged = merge_scaled_checkpoint(_checkpoint())
    assert [a.creditor_name for a in merged.accounts] == ["ALPHA", "BETA"]
    assert merged.score == 580 and merged.score_type == "FICO Score 8"
    assert merged.summary_metrics[0].value == "1"
    assert merged.inquiries[0].creditor_name == "TEST BANK"
    assert merged.inquiries[0].business_type == "Bank Credit Cards"
    assert merged.inquiries[0].contact.address == "1 MAIN ST; TESTVILLE, IA 50000"
    assert merged.inquiries[0].contact.phone == "(800) 555-0100"


def test_stage3_refuses_a_missing_batch():
    checkpoint = _checkpoint()
    checkpoint["batches"].pop("b0")
    with pytest.raises(ScaledMergeError, match="missing banked batch"):
        merge_scaled_checkpoint(checkpoint)


def test_stage3_refuses_a_batch_from_a_stale_index_plan():
    checkpoint = _checkpoint()
    checkpoint["batches"]["b0"]["plan_fingerprint"] = "stale"
    with pytest.raises(ScaledMergeError, match="different index plan"):
        merge_scaled_checkpoint(checkpoint)


@pytest.mark.parametrize('bureau', ['equifax', 'transunion', 'experian'])
def test_scaled_merge_preserves_bureau_and_raw_month_data(bureau):
    """Contract test only; this does not certify a real bureau report."""
    from app.services.document_extraction.mapping import account_row
    from app.services.document_extraction.schema import PaymentHistoryEntry

    checkpoint = _checkpoint()
    checkpoint['index']['bureau'] = bureau
    for banked in checkpoint['batches'].values():
        for account in banked['batch']['accounts']:
            account['payment_history'] = [PaymentHistoryEntry(
                year=2026, month=1, raw_status_code='30', status_code=None,
                balance='$1,234.50', past_due='$25', amount_paid='$10', amount_due='$35',
                remarks=['Synthetic monthly remark'], source_page=account['source_pages'][0],
            ).model_dump()]
    extraction = merge_scaled_checkpoint(checkpoint)
    assert extraction.bureau == bureau
    mapped = account_row(extraction.accounts[0])
    cell, = mapped['payment_history']
    assert cell['balance'] == '$1,234.50'
    assert cell['past_due'] == '$25' and cell['amount_paid'] == '$10'
    assert cell['amount_due'] == '$35' and cell['raw_status_code'] == '30'
    assert cell['source_page'] == 3 and cell['remarks'] == ['Synthetic monthly remark']
