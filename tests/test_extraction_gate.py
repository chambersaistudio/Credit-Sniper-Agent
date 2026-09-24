"""
The evaluation precondition, end to end against Postgres with the AI faked.

An account whose source text couldn't be read completely (a bare name, no
reportable fields) must NOT be sent to the reasoning engine and must never
come back "no dispute ground" — it returns need-more-evidence / extraction
incomplete, with no AI call. A complete account still evaluates normally.
"""
import httpx
import pytest

from app.services.ai_extraction import ExtractedReport
from app.services.reasoning_engine import ClaimProposalOut
from tests.conftest import requires_db

pytestmark = requires_db


def _responder(output_type, prompt, config):
    if output_type is ClaimProposalOut:
        return ClaimProposalOut(
            has_dispute_ground=False, reasoning="No issues found.", supporting_finding_ids=[],
            disputed_fields=[], recipients=[], legal_basis=[], requested_remedy=None,
            additional_evidence_needed=[], recommended_action="no_dispute", confidence=0.9,
        )
    if output_type is ExtractedReport:
        return ExtractedReport(accounts=[], inquiries=[])
    raise AssertionError(output_type)


@pytest.fixture
def ai(fake_ai):
    return fake_ai(_responder)


@pytest.fixture
async def client(db_ready, ai):
    from app.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _make_account(fields: dict, extraction_status: str = "verified") -> str:
    """Insert one canonical account (owned by the local user) with a single
    bureau record carrying `fields`. Returns the canonical account id."""
    from app.database import async_session_maker
    from app.models.canonical_account import AccountLink, CanonicalAccount
    from app.models.credit_report import CreditAccount, CreditReport
    from app.utils.default_user import get_or_create_default_user

    async with async_session_maker() as s:
        user = await get_or_create_default_user(s)
        report = CreditReport(user_id=user.id, bureau="experian", source="manual_upload", raw_text="x",
                              extraction_status=extraction_status)
        s.add(report)
        await s.flush()
        account = CreditAccount(report_id=report.id, bureau="experian", **fields)
        s.add(account)
        await s.flush()
        canonical = CanonicalAccount(user_id=user.id, creditor_name=fields.get("creditor_name") or "X")
        s.add(canonical)
        await s.flush()
        s.add(AccountLink(canonical_account_id=canonical.id, credit_account_id=account.id,
                          bureau="experian", confidence=1.0, matched_fields={}))
        await s.commit()
        return str(canonical.id)


async def test_name_only_account_blocks_evaluation(client, ai):
    canonical_id = await _make_account({"creditor_name": "PROGRESSIVE"})
    resp = await client.post(f"/api/accounts/{canonical_id}/evaluate")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["has_dispute_ground"] is False
    assert body["recommended_action"] == "need_more_evidence"  # never "no_dispute"
    assert body["additional_evidence_needed"]
    assert "extraction_incomplete" in body["validation_notes"]
    assert ai.calls == []  # the reasoning engine was never invoked


COMPLETE = {
    "creditor_name": "CAINE & WEINER", "account_number": "88XXXX2211",
    "account_status": "collection", "balance": 1204.0, "date_opened": "Feb 15, 2026",
}


async def test_complete_verified_account_is_evaluated(client, ai):
    canonical_id = await _make_account(COMPLETE, extraction_status="verified")
    resp = await client.post(f"/api/accounts/{canonical_id}/evaluate")
    assert resp.status_code == 200, resp.text
    assert len(ai.calls) >= 1  # a complete, verified record reaches the reasoning engine


@pytest.mark.parametrize("status", ["extraction_incomplete", "needs_audit", "failed"])
async def test_unverified_report_blocks_evaluation(client, ai, status):
    """Even a fully populated record is not evaluated while its report hasn't
    been verified against the original document."""
    canonical_id = await _make_account(COMPLETE, extraction_status=status)
    resp = await client.post(f"/api/accounts/{canonical_id}/evaluate")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["recommended_action"] == "need_more_evidence"
    assert body["has_dispute_ground"] is False
    expected = ("verification pass found unresolved extraction differences"
                if status == "needs_audit" else "couldn't be read completely")
    assert expected in body["reasoning"]
    # Never tell the consumer to re-upload a document that read fine.
    if status == "needs_audit":
        assert "Re-upload" not in body["reasoning"]
    assert ai.calls == []
