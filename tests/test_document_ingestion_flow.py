"""
End-to-end AI-native ingestion through the upload endpoint, with the document
provider mocked.

Asserts the architecture's central promise: the provider receives the ORIGINAL
uploaded PDF bytes (round-tripped through private storage), not text we
extracted for it — and that the two-pass result gates what happens next.

The golden expectation is the Sep 24 2026 live Experian test, reproduced with
synthetic account numbers and no consumer PII.
"""
import json
import time
import uuid
from io import BytesIO

import httpx
import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.services.ai.providers import ProviderResult
from app.services.document_extraction.schema import (
    AuditFinding, AuditReport, CreditReportExtraction, ExtractedInquiry, ExtractedTradeline, FieldEvidence,
    PaymentHistoryEntry,
)
from tests.conftest import requires_db

pytestmark = requires_db

# (creditor/collector, original creditor, type, account number, balance)
GOLDEN = [
    ("ATLAS", None, "Line of Credit", "5299XXXXXXXX1234", "$16"),
    ("CAPITAL ONE", None, "Credit card", "517805XXXXXX8842", "$842"),
    ("CREDIT ACCEPTANCE CORP", None, "Auto Loan", "7788XXXX", "$12,430"),
    ("EXTRA", None, "Line of Credit", "40011XXXXXXX2200", "$0"),
    ("MISSION LANE TAB BANK", None, "Credit card", "5178XXXXXXXX9901", "$410"),
    ("NAVY FEDERAL CR UNION", None, "Credit card", "4441XXXXXXXX1020", "$2,310"),
    ("NAVY FEDERAL CR UNION", None, "Secured loan", "9002XXXX", "$1,800"),
    ("SBNASELFLNDR", None, "Secured loan", "3301XXXX", "$520"),
    ("SELF FINANCIAL/LEAD BA", None, "Line of Credit", "3302XXXX", "$0"),
    ("CAINE & WEINER", "PROGRESSIVE", "Collection", "88XXXX2211", "$1,204"),
    ("CREDENCE RESOURCE MANA", "AT T", "Collection", "77XXXX9310", "$560"),
    ("CREDIT COLLECTION SERV", "PROGRESSIVE", "Collection", "66XXXX1188", "$980"),
    ("JEFFERSON CAPITAL SYST", "MISSION LANE CREDIT CARD", "Collection", "55XXXX7742", "$1,510"),
    ("JEFFERSON CAPITAL SYST", "T-MOBILE", "Collection", "55XXXX7743", "$420"),
    ("LVNV FUNDING LLC", "CAPITAL BANK OPEN SKY", "Collection", "44XXXX2001", "$735"),
]


def _tradeline(index, name, original, type_, number, balance) -> ExtractedTradeline:
    collection = type_ == "Collection"
    return ExtractedTradeline(
        creditor_name=name, original_creditor=original, sold_to=None, account_number=number,
        account_type=type_, open_closed="Open",
        status_raw="Collection account" if collection else "Open/Never late",
        status_normalized="collection" if collection else "open", payment_status=None,
        report_classification="Potentially negative" if collection else "Exceptional payment history",
        balance=balance, balance_updated="Jun 25, 2026", credit_limit=None if collection else "$1,000",
        original_amount=balance if collection else None, past_due_amount=balance if collection else None,
        monthly_payment=None if collection else "$16", high_balance=balance, terms=None,
        responsibility="Individual", date_opened="Dec 22, 2025", date_closed=None,
        status_updated="Jun 25, 2026", date_first_delinquency=None,
        # Like the real Experian disclosure: it prints "Balance updated" and
        # no "Last reported" field at all.
        date_last_reported=None,
        date_last_payment=None, remarks="Placed for collection" if collection else None,
        consumer_dispute=None, contact=None,
        payment_history=[PaymentHistoryEntry(year=2026, month=5, raw_status_code="OK", status_code="current",
                                             balance=balance, past_due=None, amount_paid=None,
                                             amount_due=None, remarks=[], source_page=index + 3)],
        source_pages=[index + 3], identity_evidence=f"Account name {name}",
        field_evidence=[FieldEvidence(field="account_number", value=number, page=index + 3,
                                      excerpt=f"Account number {number}")],
    )


def golden_extraction() -> CreditReportExtraction:
    return CreditReportExtraction(
        bureau="experian", report_date="Sep 24, 2026", document_created_date="Sep 24, 2026",
        consumer_on_file_since=None, score_type="FICO Score 8", score=580,
        summary_metrics=[],
        accounts=[_tradeline(i, *row) for i, row in enumerate(GOLDEN)],
        inquiries=[
            ExtractedInquiry(creditor_name="CAPITAL ONE", inquiry_date="Sep 23, 2026", inquiry_type="hard",
                             business_type="Bank Credit Cards", inquiry_category="credit_application",
                             contact=None, source_pages=[20]),
            ExtractedInquiry(creditor_name="CREDIT ONE BANK, NATIO", inquiry_date="May 15, 2026",
                             inquiry_type="hard", business_type="Bank Credit Cards",
                             inquiry_category="credit_application", contact=None, source_pages=[20]),
        ],
        public_records=[], unreadable_pages=[], warnings=[],
    )


def verifying_audit() -> AuditReport:
    return AuditReport(account_count_in_document=15, account_count_matches=True, identities_correct=True,
                       inquiries_correct=True, verified=True, findings=[], confidence=0.97)


class FakeDocumentProvider:
    """Stands in for the vendor SDK, and records exactly what it was given."""

    name = "openai"

    def __init__(self, extraction, audit):
        self._extraction = extraction
        self._audit = audit
        self.documents: list[bytes] = []
        self.calls: list[dict] = []

    async def generate(self, config, *, system, prompt, output_type, max_tokens):
        raise AssertionError("document ingestion must not fall back to the text provider")

    async def generate_document(self, config, *, system, prompt, document, filename, output_type,
                                max_tokens, detail="high"):
        self.documents.append(document)
        self.calls.append({"model": config.model, "detail": detail, "output_type": output_type,
                           "filename": filename})
        output = self._extraction if output_type is CreditReportExtraction else self._audit
        return ProviderResult(output=output, provider=self.name, model=config.model, input_tokens=100,
                              output_tokens=50, cache_read_tokens=0, cache_write_tokens=0, latency_ms=1.0)


def _pdf(text: str) -> bytes:
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    y = letter[1] - 50
    for line in text.split("\n"):
        c.drawString(50, y, line)
        y -= 14
    c.save()
    return buffer.getvalue()


SOURCE_PDF_TEXT = "EXPERIAN Credit Report\nReport date: Sep 24, 2026\nCredit Score: 580\nAccount name ATLAS Balance $16"


@pytest.fixture
def document_ai(monkeypatch):
    """Install a document provider and turn AI-native ingestion on."""
    from app.config import settings
    from app.services.ai import register_provider
    from app.services.ai.providers import _instances

    saved = dict(_instances)
    monkeypatch.setattr(settings, "document_extraction_mode", "on")
    monkeypatch.setattr(settings, "document_audit_enabled", True)

    def install(extraction=None, audit=None):
        provider = FakeDocumentProvider(extraction or golden_extraction(), audit or verifying_audit())
        register_provider("openai", provider)
        return provider

    yield install
    _instances.clear()
    _instances.update(saved)


@pytest.fixture
async def client(db_ready):
    from app.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _upload(client, pdf_bytes):
    return await client.post(
        "/api/reports/upload", files={"file": ("r.pdf", pdf_bytes, "application/pdf")},
        data={"bureau": "auto_detect"},
    )


async def test_provider_receives_the_original_pdf_bytes(client, document_ai):
    provider = document_ai()
    pdf_bytes = _pdf(SOURCE_PDF_TEXT)
    response = await _upload(client, pdf_bytes)
    assert response.status_code == 200, response.text

    # Both passes saw the original document, byte for byte — the same bytes we
    # stored privately, never a locally extracted text rendering of them.
    assert len(provider.documents) == 2
    assert all(document == pdf_bytes for document in provider.documents)
    assert [c["output_type"] for c in provider.calls] == [CreditReportExtraction, AuditReport]
    assert all(c["detail"] == "high" for c in provider.calls)


async def test_golden_experian_extraction_is_stored_and_verified(client, document_ai):
    document_ai()
    response = await _upload(client, _pdf(SOURCE_PDF_TEXT))
    body = response.json()

    assert body["extraction_status"] == "verified"
    assert body["extraction_method"] == "ai_document"
    assert body["total_accounts"] == 15
    assert body["total_inquiries"] == 2
    assert body["credit_score"] == 580
    assert body["score_type"] == "FICO Score 8"
    assert body["bureau"] == "experian"
    assert body["warnings"] == []

    detail = (await client.get(f"/api/reports/{body['report_id']}")).json()
    assert detail["report_date"] == "2026-09-24"  # calendar date, stored exactly
    by_name = {}
    for account in detail["accounts"]:
        by_name.setdefault(account["creditor_name"], []).append(account)
    # 15 tradelines across 13 names: NAVY FEDERAL and JEFFERSON each appear
    # twice, as separate accounts with their own numbers.
    assert len(detail["accounts"]) == 15
    assert len(by_name) == 13
    assert sorted(by_name) == sorted({name for name, *_ in GOLDEN})
    assert len(by_name["NAVY FEDERAL CR UNION"]) == 2
    assert {a["original_creditor"] for a in by_name["JEFFERSON CAPITAL SYST"]} == {
        "MISSION LANE CREDIT CARD", "T-MOBILE"}

    # The collector is the account; the original creditor is a separate field.
    caine = by_name["CAINE & WEINER"][0]
    assert caine["original_creditor"] == "PROGRESSIVE"
    assert caine["balance"] == 1204.0
    assert caine["account_status"] == "collection"
    assert caine["account_status_raw"] == "Collection account"
    # None of the original creditors became a tradeline of its own.
    for original in ("PROGRESSIVE", "AT T", "T-MOBILE", "CAPITAL BANK OPEN SKY", "MISSION LANE CREDIT CARD"):
        assert original not in by_name

    # Provenance survived into the record, ready for the evidence graph.
    assert caine["source_pages"]
    assert caine["field_evidence"][0]["excerpt"].startswith("Account number")
    assert caine["payment_history"][0]["raw_status_code"] == "OK"
    assert caine["payment_history"][0]["balance"] == "$1,204"
    assert caine["payment_history"][0]["source_page"] == 12

    inquiries = {i["creditor_name"]: i["inquiry_date"] for i in detail["inquiries"]}
    assert inquiries == {"CAPITAL ONE": "Sep 23, 2026", "CREDIT ONE BANK, NATIO": "May 15, 2026"}


async def test_audit_disagreement_blocks_evaluation(client, document_ai):
    disputed = AuditReport(
        account_count_in_document=15, account_count_matches=True, identities_correct=False,
        inquiries_correct=True, verified=False, confidence=0.4,
        findings=[AuditFinding(kind="correction", account_name="CAINE & WEINER", field="creditor_name",
                               extracted_value="CAINE & WEINER", correct_value="CAINE AND WEINER",
                               page=11, explanation="Name differs")],
    )
    document_ai(audit=disputed)
    body = (await _upload(client, _pdf(SOURCE_PDF_TEXT))).json()
    assert body["extraction_status"] == "needs_audit"
    # Read fine, verification disagreed — the copy must not suggest re-uploading.
    assert any("verification pass found unresolved" in w for w in body["warnings"])
    assert not any("re-upload" in w.lower() for w in body["warnings"])

    detail = (await client.get(f"/api/reports/{body['report_id']}")).json()
    assert detail["extraction_verified"] is False
    assert detail["audit_findings"]  # the disagreement is recorded, not applied

    # An unverified report cannot feed dispute analysis.
    account = (await client.get("/api/accounts/")).json()[0]
    evaluation = (await client.post(f"/api/accounts/{account['id']}/evaluate")).json()
    assert evaluation["recommended_action"] == "need_more_evidence"
    assert evaluation["has_dispute_ground"] is False


async def test_missing_accounts_make_the_report_incomplete(client, document_ai):
    incomplete = AuditReport(
        account_count_in_document=15, account_count_matches=False, identities_correct=True,
        inquiries_correct=True, verified=False, confidence=0.3,
        findings=[AuditFinding(kind="missing_account", account_name="ATLAS", field=None, extracted_value=None,
                               correct_value=None, page=3, explanation="Present in the PDF, absent here")],
    )
    document_ai(extraction=golden_extraction().model_copy(update={"accounts": [
        _tradeline(i, *row) for i, row in enumerate(GOLDEN[:6])]}), audit=incomplete)
    body = (await _upload(client, _pdf(SOURCE_PDF_TEXT))).json()
    assert body["extraction_status"] == "extraction_incomplete"
    assert body["total_accounts"] == 6


async def test_extraction_failure_does_not_lose_the_upload(client, document_ai, monkeypatch):
    document_ai()
    _provider_failure(monkeypatch, "OpenAI API error 503: unavailable")
    response = await _upload(client, _pdf(SOURCE_PDF_TEXT))
    assert response.status_code == 200
    body = response.json()
    # A provider 5xx is our outage, not an unreadable document.
    assert body["extraction_status"] == "provider_unavailable"
    assert body["total_accounts"] == 0
    # The original PDF is still stored and retrievable by its owner.
    assert (await client.get(f"/api/reports/{body['report_id']}/file")).status_code == 200


async def test_parser_cross_check_is_recorded_but_not_authoritative(client, document_ai):
    document_ai()
    body = (await _upload(client, _pdf(SOURCE_PDF_TEXT))).json()
    detail = (await client.get(f"/api/reports/{body['report_id']}")).json()
    cross_check = detail["parser_cross_check"]
    # The deterministic parser saw a different number of tradelines in this
    # one-page stand-in, and that disagreement changed nothing.
    assert cross_check["document_accounts"] == 15
    assert cross_check["agrees_on_count"] is False
    assert body["extraction_status"] == "verified"


async def test_verified_document_ingestion_permits_evaluation(client, document_ai, fake_ai):
    """The positive direction: a document reading that survives its audit is
    VERIFIED, and only then may dispute analysis run."""
    from app.services.reasoning_engine import ClaimProposalOut

    document_ai()
    # The reasoning engine runs on the text tier; stand in for it.
    reasoning = fake_ai(lambda output_type, prompt, config: ClaimProposalOut(
        has_dispute_ground=False, reasoning="Reported consistently.", supporting_finding_ids=[],
        disputed_fields=[], recipients=[], legal_basis=[], requested_remedy=None,
        additional_evidence_needed=[], recommended_action="no_dispute", confidence=0.9,
    ))

    body = (await _upload(client, _pdf(SOURCE_PDF_TEXT))).json()
    assert body["extraction_status"] == "verified"

    account = next(a for a in (await client.get("/api/accounts/")).json()
                   if a["creditor_name"] == "CAINE & WEINER")
    evaluation = (await client.post(f"/api/accounts/{account['id']}/evaluate")).json()
    assert evaluation["recommended_action"] != "need_more_evidence"
    assert len(reasoning.calls) == 1  # the reasoning engine actually ran


def live_false_positive_audit() -> AuditReport:
    """The audit from the first live Experian run: correct extraction, but two
    findings that confuse Business Type with inquiry_type, plus a complaint
    that a non-disclosed DOFD is missing."""
    return AuditReport(
        account_count_in_document=15, account_count_matches=True, identities_correct=True,
        inquiries_correct=False, verified=False, confidence=0.6,
        findings=[
            AuditFinding(kind="correction", account_name="CAPITAL ONE", field="inquiry_type",
                         extracted_value="hard", correct_value="Bank Credit Cards", page=20,
                         explanation="Business Type: Bank Credit Cards"),
            AuditFinding(kind="correction", account_name="CREDIT ONE BANK, NATIO", field="inquiry_type",
                         extracted_value="hard", correct_value="Bank Credit Cards", page=20,
                         explanation="Business Type: Bank Credit Cards"),
        ],
    )


async def test_live_experian_false_positives_no_longer_block_verification(client, document_ai):
    """The exact report that was held in NEEDS_AUDIT now reaches VERIFIED: the
    only disagreements were Business-Type-vs-inquiry_type confusions, which are
    field semantics, not disagreements about the document."""
    document_ai(audit=live_false_positive_audit())
    body = (await _upload(client, _pdf(SOURCE_PDF_TEXT))).json()

    assert body["extraction_status"] == "verified"
    assert body["total_accounts"] == 15
    assert body["warnings"] == []

    detail = (await client.get(f"/api/reports/{body['report_id']}")).json()
    # Business type is preserved in its own field; inquiry_type stays hard/soft.
    inquiries = {i["creditor_name"]: i for i in detail["inquiries"]}
    assert inquiries["CAPITAL ONE"]["business_type"] == "Bank Credit Cards"
    assert inquiries["CAPITAL ONE"]["inquiry_type"] == "hard"
    assert inquiries["CREDIT ONE BANK, NATIO"]["business_type"] == "Bank Credit Cards"
    # The findings are still on record, marked as set aside rather than applied.
    set_aside = (detail_audit := (await client.get(f"/api/reports/{body['report_id']}")).json())["audit_findings"]
    assert len(set_aside) == 2 and detail_audit["extraction_verified"] is True


async def test_balance_updated_is_not_last_reported(client, document_ai):
    document_ai()
    body = (await _upload(client, _pdf(SOURCE_PDF_TEXT))).json()
    detail = (await client.get(f"/api/reports/{body['report_id']}")).json()
    account = next(a for a in detail["accounts"] if a["creditor_name"] == "ATLAS")
    # The fixture prints only "Balance updated"; no Last reported field exists.
    assert account["balance_updated_date"] == "Jun 25, 2026"
    assert account["date_last_reported"] is None


async def test_section_labels_do_not_become_payment_status(client, document_ai):
    document_ai()
    body = (await _upload(client, _pdf(SOURCE_PDF_TEXT))).json()
    detail = (await client.get(f"/api/reports/{body['report_id']}")).json()
    for account in detail["accounts"]:
        assert account["payment_status"] not in ("Potentially negative", "Exceptional payment history")
        assert account["account_status_raw"] not in ("Potentially negative", "Exceptional payment history")
    atlas = next(a for a in detail["accounts"] if a["creditor_name"] == "ATLAS")
    assert atlas["report_classification"] == "Exceptional payment history"
    assert atlas["account_status"] == "open"  # still a real status


def transunion_extraction() -> CreditReportExtraction:
    """The same consumer at TransUnion: eleven of the fifteen Experian
    tradelines, with the bureau's own name spellings and date formats, plus
    the promotional/account-review inquiries TU discloses as non-scoring."""
    accounts = []
    for index, (name, original, type_, number, balance) in enumerate(GOLDEN[:11]):
        line = _tradeline(index, name, original, type_, number, balance)
        accounts.append(line.model_copy(update={
            "creditor_name": f"{name} NA" if not original else name,
            "date_opened": "12/22/2025",
        }))
    return CreditReportExtraction(
        bureau="transunion", report_date=None, document_created_date="09/24/2026",
        consumer_on_file_since="01/04/2011", score_type="VantageScore 3.0", score=571,
        summary_metrics=[], accounts=accounts,
        inquiries=[
            ExtractedInquiry(creditor_name="PROMO LENDER", inquiry_date="Aug 1, 2026", inquiry_type=None,
                             business_type="Bank Credit Cards", inquiry_category="promotional",
                             contact=None, source_pages=[30]),
            ExtractedInquiry(creditor_name="REVIEWING BANK", inquiry_date="Jul 1, 2026", inquiry_type=None,
                             business_type="Bank Credit Cards", inquiry_category="account_review",
                             contact=None, source_pages=[30]),
        ],
        public_records=[], unreadable_pages=[], warnings=[],
    )


async def test_second_bureau_does_not_double_the_profile(client, document_ai):
    """Experian (15) then TransUnion (11 of the same accounts) must not read
    as 26 accounts — the live symptom that exposed the matcher."""
    provider = document_ai()
    await _upload(client, _pdf(SOURCE_PDF_TEXT))

    provider._extraction = transunion_extraction()
    second = await _upload(client, _pdf("TRANSUNION disclosure\nDate Created 09/24/2026"))
    assert second.status_code == 200, second.text

    accounts = (await client.get("/api/accounts/")).json()
    # The eleven shared accounts merged; only the four Experian-only ones add.
    assert len(accounts) == 15, [a["creditor_name"] for a in accounts]
    merged = [a for a in accounts if len(a["bureaus_reporting"]) == 2]
    assert len(merged) == 11
    assert all(sorted(a["bureaus_reporting"]) == ["experian", "transunion"] for a in merged)

    dashboard = (await client.get("/api/dashboard")).json()
    assert dashboard["accounts"]["total"] == 15


async def test_transunion_report_date_uses_document_creation_not_file_since(client, document_ai):
    provider = document_ai()
    provider._extraction = transunion_extraction()
    body = (await _upload(client, _pdf("TRANSUNION disclosure\nDate Created 09/24/2026"))).json()

    detail = (await client.get(f"/api/reports/{body['report_id']}")).json()
    # Date Created, not the 2011 "on file since" date.
    assert detail["report_date"] == "2026-09-24"
    assert detail["on_file_since"] == "01/04/2011"


async def test_promotional_and_account_review_inquiries_are_not_hard(client, document_ai):
    provider = document_ai()
    provider._extraction = transunion_extraction()
    body = (await _upload(client, _pdf("TRANSUNION disclosure\nDate Created 09/24/2026"))).json()

    detail = (await client.get(f"/api/reports/{body['report_id']}")).json()
    by_name = {i["creditor_name"]: i for i in detail["inquiries"]}
    assert by_name["PROMO LENDER"]["inquiry_category"] == "promotional"
    assert by_name["REVIEWING BANK"]["inquiry_category"] == "account_review"
    # Consumer-visible, non-scoring: never counted as hard inquiries.
    assert all(i["inquiry_type"] == "soft" for i in detail["inquiries"])
    assert not any(i["inquiry_type"] == "hard" for i in detail["inquiries"])


# ── Provider failure is not a document failure ─────────────────────────────

# What OpenAI actually returned in the live test.
QUOTA_ERROR = (
    "OpenAI API error 429: You exceeded your current quota, please check your plan and billing details. "
    "(insufficient_quota / credit_balance_exhausted)"
)


def _provider_failure(monkeypatch, message=QUOTA_ERROR):
    """Make the document provider fail the way OpenAI did live. Returns a
    callable that restores just this patch, leaving the fixture's own
    configuration (AI ingestion enabled) in place."""
    from app.services import document_extraction
    from app.services.ai import AIProviderError

    original = document_extraction.pipeline.generate_document

    async def boom(*args, **kwargs):
        raise AIProviderError(message)

    monkeypatch.setattr(document_extraction.pipeline, "generate_document", boom)

    def restore():
        monkeypatch.setattr(document_extraction.pipeline, "generate_document", original)

    return restore


async def test_quota_exhausted_is_reported_as_provider_unavailable(client, document_ai, monkeypatch):
    document_ai()
    _provider_failure(monkeypatch)
    body = (await _upload(client, _pdf(SOURCE_PDF_TEXT))).json()

    assert body["extraction_status"] == "provider_unavailable"
    assert body["extraction_method"] == "provider_unavailable"
    warning = " ".join(body["warnings"])
    assert "stored safely" in warning and "temporarily unavailable" in warning
    assert "Retry extraction once the service is available" in warning
    # Never blames the document, and never claims it holds no accounts.
    for wrong in ("couldn't reliably read this PDF", "text-based PDF", "No accounts could be read"):
        assert wrong not in warning


async def test_provider_internals_never_reach_the_consumer(client, document_ai, monkeypatch):
    document_ai()
    _provider_failure(monkeypatch)
    body = (await _upload(client, _pdf(SOURCE_PDF_TEXT))).json()
    detail = (await client.get(f"/api/reports/{body['report_id']}")).json()

    leaked = ("429", "insufficient_quota", "credit_balance_exhausted", "quota", "billing",
              "AIProviderError", "OpenAI")
    payload = json.dumps([body, detail])
    for token in leaked:
        assert token not in payload, f"provider internals leaked: {token}"


async def test_provider_error_is_kept_for_operators(client, document_ai, monkeypatch):
    """The detail a maintainer needs is recorded on the report, just not in
    any consumer-facing response."""
    from app.database import async_session_maker
    from app.models.credit_report import CreditReport

    document_ai()
    _provider_failure(monkeypatch)
    body = (await _upload(client, _pdf(SOURCE_PDF_TEXT))).json()

    async with async_session_maker() as session:
        report = await session.get(CreditReport, uuid.UUID(body["report_id"]))
        assert "insufficient_quota" in report.extraction_audit["provider_error"]


async def test_stored_original_survives_and_analysis_stays_blocked(client, document_ai, monkeypatch):
    document_ai()
    _provider_failure(monkeypatch)
    body = (await _upload(client, _pdf(SOURCE_PDF_TEXT))).json()

    # The original PDF is safe and retrievable.
    stored = await client.get(f"/api/reports/{body['report_id']}/file")
    assert stored.status_code == 200 and stored.content.startswith(b"%PDF")
    # And nothing is analyzable yet.
    assert (await client.get("/api/accounts/")).json() == []


async def test_retry_extraction_uses_the_stored_original(client, document_ai, monkeypatch):
    """The outage clears and the consumer retries — without re-uploading."""
    provider = document_ai()
    restore = _provider_failure(monkeypatch)
    body = (await _upload(client, _pdf(SOURCE_PDF_TEXT))).json()
    assert body["extraction_status"] == "provider_unavailable"

    detail = (await client.get(f"/api/reports/{body['report_id']}")).json()
    assert detail["extraction_retryable"] is True

    # The outage clears; the mocked provider serves normally again.
    restore()
    retried = await client.post(f"/api/reports/{body['report_id']}/retry-extraction")
    assert retried.status_code == 200, retried.text
    result = retried.json()
    assert result["extraction_status"] == "verified"
    assert result["total_accounts"] == 15

    # It re-read the same stored bytes rather than asking for the file again.
    assert provider.documents and all(d == provider.documents[0] for d in provider.documents)
    accounts = (await client.get("/api/accounts/")).json()
    assert len(accounts) == 15


async def test_retry_is_refused_once_a_report_has_accounts(client, document_ai):
    document_ai()
    body = (await _upload(client, _pdf(SOURCE_PDF_TEXT))).json()
    assert body["extraction_status"] == "verified"
    refused = await client.post(f"/api/reports/{body['report_id']}/retry-extraction")
    assert refused.status_code == 409


async def test_retry_requires_ownership(client, document_ai, monkeypatch):
    document_ai()
    _provider_failure(monkeypatch)
    body = (await _upload(client, _pdf(SOURCE_PDF_TEXT))).json()
    missing = await client.post(f"/api/reports/{uuid.uuid4()}/retry-extraction")
    assert missing.status_code == 404
