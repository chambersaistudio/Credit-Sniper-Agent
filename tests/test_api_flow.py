"""
End-to-end API flow against a real Postgres (TEST_DATABASE_URL) with the
AI provider faked. Exercises the path a consumer takes on their phone:
upload → profile → evaluate → case → package → approve → send → response.
"""
import re
from io import BytesIO

import httpx
import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from sqlalchemy import func, select

from app.services.ai import add_usage_listener
from app.services.ai_extraction import ExtractedAccount, ExtractedInquiry, ExtractedReport
from app.services.reasoning_engine import ClaimProposalOut
from tests.conftest import requires_db

pytestmark = requires_db

EQUIFAX = """EQUIFAX Credit Report
Report Date: January 15, 2024
Credit Score: 620

ACCOUNTS

Creditor: CAPITAL ONE BANK
Account Number: XXXX-XXXX-XXXX-4521
Account Type: Credit Card
Account Status: Charged Off
Balance: $3,450.00
High Balance: $5,000.00
Credit Limit: $2,000.00
Date Opened: 03/2015
Date of First Delinquency: 06/2016

Creditor: MIDLAND CREDIT MANAGEMENT
Account Number: XXXX-9988
Account Type: Collection
Account Status: Open Collection
Balance: $890.00
Date Opened: 01/2022
Date of First Delinquency: 05/2016

INQUIRIES

Chase Bank - Date: 01/15/2024
"""

EXPERIAN = """EXPERIAN Credit Report
Report Date: January 20, 2024
Credit Score: 615

ACCOUNTS

Creditor: CAPITAL ONE
Account Number: XXXX-4521
Account Type: Credit Card
Account Status: Charged Off
Balance: $3,200.00
Date Opened: 03/2015
Date of First Delinquency: 06/2018
"""

UNSTRUCTURED = """TRANSUNION personal credit report
Report Date: February 1, 2024
Tradelines
DISCOVER BANK   6011********7788   revolving   opened 04/2019   bal 1,250   pays as agreed
"""


def _pdf(text: str) -> bytes:
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    y = letter[1] - 50
    for line in text.split("\n"):
        c.drawString(50, y, line)
        y -= 14
    c.save()
    return buffer.getvalue()


def _responder(output_type, prompt, config):
    if output_type is ClaimProposalOut:
        finding_ids = re.findall(r'"id": "(F\d+)"', prompt)
        if not finding_ids:
            return ClaimProposalOut(
                has_dispute_ground=False, reasoning="No issues found.", supporting_finding_ids=[],
                disputed_fields=[], recipients=[], legal_basis=[], requested_remedy=None,
                additional_evidence_needed=[], recommended_action="no_dispute", confidence=0.9,
            )
        bureaus = [b for b in ("equifax", "experian", "transunion") if f'"bureau": "{b}"' in prompt]
        return ClaimProposalOut(
            has_dispute_ground=True, reasoning="The reported dates conflict.", supporting_finding_ids=finding_ids,
            disputed_fields=["date_of_first_delinquency"], recipients=bureaus + ["furnisher"],
            legal_basis=[{"reference_id": "fcra_611_reinvestigation", "applies_because": "Disputed accuracy"}],
            requested_remedy="Correct the Date of First Delinquency or delete the account.",
            additional_evidence_needed=[], recommended_action="dispute_both", confidence=0.85,
        )
    if output_type is ExtractedReport:
        return ExtractedReport(
            accounts=[ExtractedAccount(
                source_excerpt="DISCOVER BANK 6011********7788", creditor_name="DISCOVER BANK",
                account_number="6011********7788", account_type="revolving", account_status="pays as agreed",
                payment_status=None, balance="1,250", past_due_amount="$999",  # not in the document
                credit_limit=None, high_balance=None, date_opened="04/2019", date_closed=None,
                date_of_first_delinquency=None, date_last_reported=None, date_last_payment=None, remarks=None,
            ), ExtractedAccount(
                source_excerpt="WELLS FARGO 1234", creditor_name="WELLS FARGO",  # invented account
                account_number="1234", account_type=None, account_status=None, payment_status=None,
                balance=None, past_due_amount=None, credit_limit=None, high_balance=None, date_opened=None,
                date_closed=None, date_of_first_delinquency=None, date_last_reported=None,
                date_last_payment=None, remarks=None,
            )],
            inquiries=[ExtractedInquiry(creditor_name="Invented Lender", inquiry_date=None)],
        )
    raise AssertionError(f"unexpected output type {output_type}")


@pytest.fixture
async def client(db_ready, fake_ai):
    fake_ai(_responder)
    from app.main import app
    from app.services.usage_sink import persist_usage

    add_usage_listener(persist_usage)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _upload(client, text, bureau="auto_detect"):
    response = await client.post(
        "/api/reports/upload", files={"file": ("r.pdf", _pdf(text), "application/pdf")}, data={"bureau": bureau},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_full_consumer_flow(client):
    me = (await client.get("/api/users/me")).json()
    assert me["missing_for_correspondence"]

    equifax = await _upload(client, EQUIFAX)
    assert (equifax["bureau"], equifax["total_accounts"], equifax["total_inquiries"]) == ("equifax", 2, 1)
    experian = await _upload(client, EXPERIAN)
    assert experian["bureau"] == "experian"

    accounts = (await client.get("/api/accounts/")).json()
    capital_one = next(a for a in accounts if a["creditor_name"] == "CAPITAL ONE BANK")
    assert capital_one["bureaus_reporting"] == ["equifax", "experian"]
    rules = {f["rule"] for f in capital_one["findings"]}
    assert {"cross_bureau.balance", "cross_bureau.dofd"} <= rules
    equifax_record = next(r for r in capital_one["records"] if r["bureau"] == "equifax")
    assert (equifax_record["balance"], equifax_record["high_balance"]) == (3450.0, 5000.0)

    evaluation = (await client.post(f"/api/accounts/{capital_one['id']}/evaluate")).json()
    assert evaluation["has_dispute_ground"] and evaluation["evidence"]

    response = await client.post("/api/cases/", json={"claim_ids": [evaluation["id"]], "recipient": "equifax"})
    assert response.status_code == 200, response.text
    case = response.json()
    assert case["status"] == "draft"
    duplicate = await client.post("/api/cases/", json={"claim_ids": [evaluation["id"]], "recipient": "equifax"})
    assert duplicate.status_code == 409

    case = (await client.post(f"/api/cases/{case['id']}/package")).json()
    assert case["status"] == "awaiting_approval"
    assert not case["package"]["ready"]  # profile incomplete
    assert (await client.post(f"/api/cases/{case['id']}/approve")).status_code == 409

    await client.patch("/api/users/me", json={
        "email": "jane@example.com", "full_name": "Jane Consumer", "address": "1 Main St",
        "city": "Anytown", "state": "ca", "zip_code": "90210", "ssn_last_four": "1234",
    })
    case = (await client.post(f"/api/cases/{case['id']}/package")).json()
    package = case["package"]
    assert package["ready"]
    assert "ending 4521" in package["subject"]
    assert "Equifax Information Services LLC reports $3,450.00" in package["body"]
    assert "XXX-XX-1234" in package["body"]

    pdf = await client.get(f"/api/cases/{case['id']}/package.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")

    assert (await client.post(f"/api/cases/{case['id']}/submitted", json={"channel": "certified_mail"})).status_code == 409
    case = (await client.post(f"/api/cases/{case['id']}/approve")).json()
    case = (await client.post(f"/api/cases/{case['id']}/submitted", json={
        "channel": "certified_mail", "submitted_at": "2024-02-01T12:00:00Z", "tracking_number": "9400",
    })).json()
    assert (case["status"], case["deadline_basis"]) == ("submitted", "submitted_estimate")
    assert case["response_due_at"].startswith("2024-03-02")
    assert case["overdue"]  # that date has passed with no response recorded

    case = (await client.post(f"/api/cases/{case['id']}/delivered", json={"delivered_at": "2024-02-05T12:00:00Z"})).json()
    assert (case["deadline_basis"], case["response_due_at"][:10]) == ("delivered", "2024-03-06")

    case = (await client.post(f"/api/cases/{case['id']}/response", json={"outcome": "verified", "detail": "Bureau verified"})).json()
    assert case["status"] == "verified"
    case = (await client.post(f"/api/cases/{case['id']}/transition", json={"target": "resolved"})).json()
    assert case["status"] == "resolved"
    statuses = [(e["from_status"], e["to_status"]) for e in case["events"] if e["event_type"] == "status_change"]
    assert ("delivered", "response_received") in statuses and ("response_received", "verified") in statuses
    bad = await client.post(f"/api/cases/{case['id']}/transition", json={"target": "monitoring"})
    assert bad.status_code == 409

    dashboard = (await client.get("/api/dashboard")).json()
    assert dashboard["scores"].get("equifax", {}).get("score") == 620, dashboard
    assert dashboard["cases"]["resolved"] == 1
    activity = (await client.get("/api/activity")).json()
    assert any(i["type"] == "report_uploaded" for i in activity) and any(i["type"] == "case_event" for i in activity)

    from app.database import async_session_maker
    from app.models.ai_usage import AIUsageLog
    async with async_session_maker() as session:
        logged = (await session.execute(select(func.count()).select_from(AIUsageLog))).scalar()
    assert logged >= 1


async def test_reevaluation_supersedes_and_stale_claims_are_rejected(client):
    await _upload(client, EQUIFAX)
    await _upload(client, EXPERIAN)
    account = next(a for a in (await client.get("/api/accounts/")).json() if a["creditor_name"] == "CAPITAL ONE BANK")
    first = (await client.post(f"/api/accounts/{account['id']}/evaluate")).json()
    await client.post(f"/api/accounts/{account['id']}/evaluate")
    stale = await client.post("/api/cases/", json={"claim_ids": [first["id"]], "recipient": "equifax"})
    assert stale.status_code == 409


async def test_no_dispute_ground_means_no_case(client):
    clean = EXPERIAN.replace("Charged Off", "Open").replace("Date of First Delinquency: 06/2018\n", "")
    await _upload(client, clean)
    account = (await client.get("/api/accounts/")).json()[0]
    assert account["findings"] == []
    evaluation = (await client.post(f"/api/accounts/{account['id']}/evaluate")).json()
    assert evaluation["recommended_action"] == "no_dispute"
    refused = await client.post("/api/cases/", json={"claim_ids": [evaluation["id"]], "recipient": "experian"})
    assert refused.status_code == 422


async def test_ai_extraction_fallback_drops_ungrounded_values(client):
    report = await _upload(client, UNSTRUCTURED, bureau="transunion")
    assert report["extraction_method"] == "ai_verified"
    assert report["total_accounts"] == 1  # the invented Wells Fargo account was dropped
    assert report["total_inquiries"] == 0  # the invented inquiry was dropped
    detail = (await client.get(f"/api/reports/{report['report_id']}")).json()
    account = detail["accounts"][0]
    assert (account["creditor_name"], account["balance"]) == ("DISCOVER BANK", 1250.0)
    assert account["past_due_amount"] is None  # "$999" never appears in the document
    assert any("left blank" in w for w in report["warnings"])


async def test_upload_rejects_non_pdf_and_ambiguous_bureau(client):
    not_pdf = await client.post("/api/reports/upload", files={"file": ("r.pdf", b"hello", "application/pdf")})
    assert not_pdf.status_code == 400
    ambiguous = await client.post(
        "/api/reports/upload", files={"file": ("r.pdf", _pdf("Credit report\nCreditor: X\nAccount Number: 1234"), "application/pdf")},
    )
    assert ambiguous.status_code == 422
