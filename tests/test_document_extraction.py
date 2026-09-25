"""
AI-native document ingestion: the model reads the ORIGINAL PDF.

Covers the request actually sent to the provider (raw PDF bytes, no stored
response, no conversation), the strict schema, the two-pass reconciliation,
and the guarantee that no report content reaches the logs.

The provider is mocked throughout — live provider evaluation is opt-in and
lives outside this suite.
"""
import base64
import json
import logging

import pytest
from pydantic import ValidationError

from app.services.ai import ModelTier, resolve_tier
from app.services.ai.providers import OpenAIProvider
from app.services.document_extraction import ExtractionStatus, reconcile
from app.services.document_extraction.mapping import account_row, inquiry_rows, money, normalized_status
from app.services.document_extraction.schema import (
    AuditFinding, AuditReport, ContactInfo, CreditReportExtraction, ExtractedInquiry,
    ExtractedTradeline, FieldEvidence, PaymentHistoryEntry,
)

PDF_BYTES = b"%PDF-1.4 synthetic original document bytes"


def tradeline(**kw) -> ExtractedTradeline:
    base = dict(
        creditor_name="CAINE & WEINER", original_creditor="PROGRESSIVE", sold_to=None,
        account_number="88XXXX2211", account_type="Collection", open_closed="Open",
        status_raw="Collection account", status_normalized="collection", payment_status=None,
        report_classification="Potentially negative",
        balance="$1,204", balance_updated="May 10, 2026", credit_limit=None,
        original_amount="$1,204", past_due_amount="$1,204", monthly_payment=None,
        high_balance="$1,204", terms=None, responsibility="Individual",
        date_opened="Feb 15, 2026", date_closed=None, status_updated="May 10, 2026",
        date_first_delinquency=None, date_last_reported="May 10, 2026", date_last_payment=None,
        remarks="Placed for collection", consumer_dispute=None,
        contact=ContactInfo(name="Caine & Weiner", address="PO Box 1, Woodland Hills CA", phone="800-555-0100"),
        payment_history=[PaymentHistoryEntry(year=2026, month=4, raw_status_code="CO", status_code="charged_off",
                            balance="$1,204", past_due="$1,204", amount_paid=None, amount_due=None,
                            remarks=["Placed for collection"], source_page=11)],
        source_pages=[11],
        identity_evidence="Account name CAINE & WEINER",
        field_evidence=[FieldEvidence(field="original_creditor", value="PROGRESSIVE", page=11,
                                      excerpt="Original creditor PROGRESSIVE")],
    )
    base.update(kw)
    return ExtractedTradeline(**base)


def extraction(**kw) -> CreditReportExtraction:
    base = dict(
        bureau="experian", report_date="Sep 24, 2026", document_created_date="Sep 24, 2026",
        consumer_on_file_since=None, score_type="FICO Score 8", score=580,
        summary_metrics=[], accounts=[tradeline()],
        inquiries=[ExtractedInquiry(creditor_name="CAPITAL ONE", inquiry_date="Sep 23, 2026",
                                    inquiry_type="hard", business_type="Bank Credit Cards",
                                    inquiry_category="credit_application", contact=None, source_pages=[20])],
        public_records=[], unreadable_pages=[], warnings=[],
    )
    base.update(kw)
    return CreditReportExtraction(**base)


def audit(**kw) -> AuditReport:
    base = dict(account_count_in_document=1, account_count_matches=True, identities_correct=True,
                inquiries_correct=True, verified=True, findings=[], confidence=0.95)
    base.update(kw)
    return AuditReport(**base)


# ── The request we actually send the provider ──────────────────────────────

def test_document_request_carries_the_original_pdf_inline():
    provider = OpenAIProvider.__new__(OpenAIProvider)  # no SDK client needed
    payload = provider.build_document_input("Transcribe it.", PDF_BYTES, "report.pdf", "high")

    assert len(payload) == 1 and payload[0]["role"] == "user"
    file_part, text_part = payload[0]["content"]
    assert file_part["type"] == "input_file"
    assert file_part["filename"] == "report.pdf"
    assert file_part["detail"] == "high"
    # The ORIGINAL bytes, inline — not text we extracted, and not a persistent
    # Files object that would outlive the request.
    prefix = "data:application/pdf;base64,"
    assert file_part["file_data"].startswith(prefix)
    assert base64.b64decode(file_part["file_data"][len(prefix):]) == PDF_BYTES
    assert text_part == {"type": "input_text", "text": "Transcribe it."}


def test_document_tiers_are_environment_configurable(monkeypatch):
    from app.config import settings

    assert resolve_tier(ModelTier.DOCUMENT_EXTRACTION).provider == "openai"
    monkeypatch.setattr(settings, "ai_document_extraction_provider", "anthropic")
    monkeypatch.setattr(settings, "ai_document_extraction_model", "some-other-model")
    config = resolve_tier(ModelTier.DOCUMENT_EXTRACTION)
    assert (config.provider, config.model) == ("anthropic", "some-other-model")


# ── Strict schema ──────────────────────────────────────────────────────────

def test_schema_requires_every_field_even_when_null():
    # Strict structured outputs: keys must be present; absence is an error,
    # not an implicit null.
    with pytest.raises(ValidationError):
        ExtractedTradeline(creditor_name="ATLAS")


def test_schema_keeps_furnisher_and_original_creditor_separate():
    line = tradeline()
    assert line.creditor_name == "CAINE & WEINER"
    assert line.original_creditor == "PROGRESSIVE"
    row = account_row(line)
    assert row["creditor_name"] == "CAINE & WEINER"
    assert row["original_creditor"] == "PROGRESSIVE"
    # The live bug: a collection stored under its original creditor's name.
    assert row["creditor_name"] != row["original_creditor"]


def test_mapping_converts_printed_values_deterministically():
    row = account_row(tradeline())
    assert row["balance"] == 1204.0 and row["past_due_amount"] == 1204.0
    assert row["account_status"] == "collection"
    assert row["account_status_raw"] == "Collection account"
    assert row["date_last_reported"] == "May 10, 2026"
    # Every per-month field the report printed survives, not just the code.
    assert row["payment_history"] == [{
        "year": 2026, "month": 4, "status_code": "charged_off", "raw_status_code": "CO",
        "balance": "$1,204", "past_due": "$1,204", "amount_paid": None, "amount_due": None,
        "remarks": ["Placed for collection"], "source_page": 11,
    }]
    assert row["source_pages"] == [11]
    assert row["field_evidence"][0]["excerpt"] == "Original creditor PROGRESSIVE"
    assert row["contact"]["phone"] == "800-555-0100"


def test_mapping_never_invents_values():
    row = account_row(tradeline(balance=None, status_raw=None, status_normalized=None, open_closed=None,
                                payment_status=None))
    assert row["balance"] is None
    assert row["account_status"] is None
    assert money(None) is None and money("not a number") is None


def test_normalized_status_falls_back_to_the_printed_wording():
    # A model-supplied normalization we don't recognize is discarded in favour
    # of one derived from what the document actually printed.
    line = tradeline(status_normalized="totally-made-up", status_raw="Charged off")
    assert normalized_status(line) == "charged_off"


def test_inquiry_rows_skip_blank_names():
    report = extraction(inquiries=[
        ExtractedInquiry(creditor_name="CAPITAL ONE", inquiry_date="Sep 23, 2026", inquiry_type=None,
                         business_type="Bank Credit Cards", inquiry_category=None,
                         contact=None, source_pages=[20]),
        ExtractedInquiry(creditor_name="   ", inquiry_date=None, inquiry_type=None, business_type=None,
                         inquiry_category=None, contact=None, source_pages=[]),
    ])
    rows = inquiry_rows(report)
    # No category and no stated type: the type stays null rather than
    # becoming a hard inquiry the consumer never incurred.
    assert rows == [{"creditor_name": "CAPITAL ONE", "inquiry_date": "Sep 23, 2026",
                     "inquiry_type": None, "inquiry_category": None,
                     "business_type": "Bank Credit Cards"}]


# ── Two-pass reconciliation ────────────────────────────────────────────────

def test_clean_two_pass_agreement_verifies():
    status, reasons = reconcile(extraction(), audit())
    assert status is ExtractionStatus.VERIFIED and reasons == []
    assert status.allows_evaluation


def test_missing_audit_is_never_verified():
    status, reasons = reconcile(extraction(), None)
    assert status is ExtractionStatus.NEEDS_AUDIT
    assert not status.allows_evaluation
    assert any("independent audit" in r for r in reasons)


def test_audit_disagreement_is_not_silently_accepted():
    disagreement = audit(verified=False, findings=[
        AuditFinding(kind="correction", account_name="CAINE & WEINER", field="balance",
                     extracted_value="$1,204", correct_value="$1,240", page=11, explanation="Digits transposed"),
    ])
    status, reasons = reconcile(extraction(), disagreement)
    assert status is ExtractionStatus.NEEDS_AUDIT
    assert not status.allows_evaluation
    assert any("corrected 1 field" in r for r in reasons)


def test_missing_account_makes_extraction_incomplete():
    missing = audit(account_count_in_document=15, account_count_matches=False, verified=False, findings=[
        AuditFinding(kind="missing_account", account_name="ATLAS", field=None, extracted_value=None,
                     correct_value=None, page=3, explanation="Not present in the extraction"),
    ])
    status, reasons = reconcile(extraction(), missing)
    assert status is ExtractionStatus.EXTRACTION_INCOMPLETE
    assert any("counted 15 tradelines" in r for r in reasons)
    assert any("missing from the extraction" in r for r in reasons)


def test_wrong_identity_blocks_verification():
    # Exactly the live failure: a collection recorded under its original
    # creditor's name. The auditor says identities are wrong; we withhold it.
    status, reasons = reconcile(
        extraction(accounts=[tradeline(creditor_name="PROGRESSIVE", original_creditor=None)]),
        audit(identities_correct=False, verified=False),
    )
    assert status is ExtractionStatus.NEEDS_AUDIT
    assert any("account identities" in r for r in reasons)


def test_duplicate_tradelines_are_detected_deterministically():
    status, reasons = reconcile(extraction(accounts=[tradeline(), tradeline()]), audit(account_count_in_document=2))
    assert status is ExtractionStatus.EXTRACTION_INCOMPLETE
    assert any("Duplicate tradelines" in r for r in reasons)


def test_empty_extraction_is_incomplete():
    status, reasons = reconcile(extraction(accounts=[]), audit())
    assert status is ExtractionStatus.EXTRACTION_INCOMPLETE
    assert reasons == ["No tradelines were extracted from the document."]


def test_unreadable_pages_and_missing_provenance_block_verification():
    status, reasons = reconcile(
        extraction(unreadable_pages=[7], accounts=[tradeline(source_pages=[])]), audit())
    assert status is ExtractionStatus.NEEDS_AUDIT
    assert any("could not be read reliably" in r for r in reasons)
    assert any("no source page recorded" in r for r in reasons)


# ── Logging safety ─────────────────────────────────────────────────────────

async def test_document_content_never_reaches_the_logs(caplog, monkeypatch):
    """A provider failure must not spill the PDF, the base64 payload, or the
    request body into application logs."""
    from app.services import document_extraction
    from app.services.ai import AIProviderError

    secret = "CORNELIUS CHAMBERS 987-65-4321"
    document = b"%PDF-1.4 " + secret.encode() + b" more report content"

    async def boom(*args, **kwargs):
        raise AIProviderError("OpenAI API error 500: upstream failure")

    monkeypatch.setattr(document_extraction.pipeline, "generate_document", boom)
    with caplog.at_level(logging.DEBUG):
        result = await document_extraction.extract_document(document, context={"user_id": "u"})

    # A provider error is an outage, not an unreadable document.
    assert result.status is ExtractionStatus.PROVIDER_UNAVAILABLE
    # The provider's own message is kept for operators...
    assert "upstream failure" in result.provider_error
    # ...but the consumer-facing reason says nothing about their PDF.
    assert result.reasons == ["AI extraction was unavailable, so the document was never analyzed."]
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in logged
    assert base64.b64encode(document).decode()[:24] not in logged
    assert "%PDF" not in logged


def test_document_model_costs_are_estimated():
    """Document calls are token-heavy; they must land in the usage log with a
    cost, not as an unpriced unknown model."""
    from app.services.ai.config import estimate_cost_usd

    # 1M input + 1M output at the Sol rates.
    assert estimate_cost_usd("gpt-5.6-sol", 1_000_000, 1_000_000) == 24.0
    assert estimate_cost_usd("gpt-5.6-terra", 1_000_000, 1_000_000) == 14.0
    assert estimate_cost_usd("gpt-5.6-luna", 1_000_000, 1_000_000) == 1.4
    # The defaults both resolve to a priced model.
    for tier in (ModelTier.DOCUMENT_EXTRACTION, ModelTier.DOCUMENT_AUDIT):
        config = resolve_tier(tier)
        assert config.model == "gpt-5.6-sol"
        assert estimate_cost_usd(config.model, 1000, 100) is not None
