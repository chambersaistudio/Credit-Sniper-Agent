"""Deterministic Stage-3 merge tests. No network/provider calls."""
import pytest

from app.services.batch_job import plan_fingerprint
from app.services.document_extraction.batch_schema import TradelineBatch
from app.services.document_extraction.batching import plan_batches
from app.services.document_extraction.index_schema import (
    IndexedInquiry, IndexedSummaryMetric, IndexedTradeline, ReportIndex,
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
            business_type="Bank Credit Cards", source_pages=[19],
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
        date_of_first_delinquency=None, date_last_reported=None, date_last_payment=None,
        date_last_active=None, date_status_updated=None, balance_updated=None,
        remarks=None, contact=None, payment_history=[], source_pages=[page],
        field_evidence=[],
    )


def _checkpoint():
    index = _index()
    plans = plan_batches(index, batch_size=1, context_pages=0)
    batches = {}
    for plan, account in zip(plans, [_account("ALPHA", "1111XX", 3), _account("BETA", "2222XX", 4)]):
        batch = TradelineBatch(accounts=[account], missing_tradelines=[], unreadable_pages=[])
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
    assert merged.inquiries[0].contact is None


def test_stage3_refuses_a_missing_batch():
    checkpoint = _checkpoint()
    checkpoint["batches"].pop("b1")
    with pytest.raises(ScaledMergeError, match="missing banked batch"):
        merge_scaled_checkpoint(checkpoint)


def test_stage3_refuses_a_batch_from_a_stale_index_plan():
    checkpoint = _checkpoint()
    checkpoint["batches"]["b0"]["plan_fingerprint"] = "stale"
    with pytest.raises(ScaledMergeError, match="different index plan"):
        merge_scaled_checkpoint(checkpoint)
