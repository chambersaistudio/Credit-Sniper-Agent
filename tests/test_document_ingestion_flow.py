"""
End-to-end AI-native ingestion through the upload endpoint, with the document
provider mocked.

Asserts the architecture's central promise: the provider receives the ORIGINAL
uploaded PDF bytes (round-tripped through private storage), not text we
extracted for it — and that the two-pass result gates what happens next.

The golden expectation is the Sep 24 2026 live Experian test, reproduced with
synthetic account numbers and no consumer PII.
"""
import time
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
        balance=balance, balance_updated="Jun 25, 2026", credit_limit=None if collection else "$1,000",
        original_amount=balance if collection else None, past_due_amount=balance if collection else None,
        monthly_payment=None if collection else "$16", high_balance=balance, terms=None,
        responsibility="Individual", date_opened="Dec 22, 2025", date_closed=None,
        status_updated="Jun 25, 2026", date_first_delinquency=None, date_last_reported="Jun 25, 2026",
        date_last_payment=None, remarks="Placed for collection" if collection else None,
        consumer_dispute=None, contact=None,
        payment_history=[PaymentHistoryEntry(year=2026, month=5, raw_code="OK", code="current")],
        source_pages=[index + 3], identity_evidence=f"Account name {name}",
        field_evidence=[FieldEvidence(field="account_number", value=number, page=index + 3,
                                      excerpt=f"Account number {number}")],
    )


def golden_extraction() -> CreditReportExtraction:
    return CreditReportExtraction(
        bureau="experian", report_date="Sep 24, 2026", score_type="FICO Score 8", score=580,
        summary_metrics=[],
        accounts=[_tradeline(i, *row) for i, row in enumerate(GOLDEN)],
        inquiries=[
            ExtractedInquiry(creditor_name="CAPITAL ONE", inquiry_date="Sep 23, 2026", inquiry_type="hard",
                             contact=None, source_pages=[20]),
            ExtractedInquiry(creditor_name="CREDIT ONE BANK, NATIO", inquiry_date="May 15, 2026",
                             inquiry_type="hard", contact=None, source_pages=[20]),
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
    assert caine["payment_history"] == [{"year": 2026, "month": 5, "raw_code": "OK", "code": "current"}]

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
    assert any("not verified" in w for w in body["warnings"])

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
    from app.services import document_extraction
    from app.services.ai import AIProviderError

    document_ai()

    async def boom(*args, **kwargs):
        raise AIProviderError("OpenAI API error 503: unavailable")

    monkeypatch.setattr(document_extraction.pipeline, "generate_document", boom)
    response = await _upload(client, _pdf(SOURCE_PDF_TEXT))
    assert response.status_code == 200
    body = response.json()
    assert body["extraction_status"] == "failed"
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
