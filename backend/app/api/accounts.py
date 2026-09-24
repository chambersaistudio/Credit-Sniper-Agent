"""
The consumer's normalized credit profile: one entry per real-world account,
its current record per bureau, deterministic findings, the latest
evaluation (claim), and any cases.
"""
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import current_user, current_user_id
from app.database import get_db
from app.models.case import Case, Claim
from app.models.user import User
from app.services.ai import ModelTier
from app.services.case_service import save_evaluation
from app.services.credit_profile import AccountView, load_account, load_profile, view_to_dict
from app.services.document_extraction import ExtractionStatus
from app.services.extraction_quality import record_is_disputable
from app.services.legal_references import REFERENCES
from app.services.reasoning_engine import ClaimProposal, evaluate_account
from app.services.redaction import Identity
from app.utils.default_user import parse_uuid

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def claim_to_dict(claim: Claim) -> dict[str, Any]:
    return {
        "id": str(claim.id),
        "has_dispute_ground": claim.has_dispute_ground,
        "recommended_action": claim.recommended_action,
        "reasoning": claim.reasoning,
        "disputed_fields": claim.disputed_fields or [],
        "recipients": claim.recipients or [],
        "legal_basis": [
            {"id": rid, "citation": REFERENCES[rid].citation, "title": REFERENCES[rid].title,
             "applies_because": (claim.legal_explanations or {}).get(rid)}
            for rid in (claim.legal_reference_ids or []) if rid in REFERENCES
        ],
        "requested_remedy": claim.requested_remedy,
        "additional_evidence_needed": claim.additional_evidence_needed or [],
        "confidence": claim.confidence,
        "model_tier": claim.model_tier,
        "status": claim.status,
        "created_at": claim.created_at.isoformat() if claim.created_at else None,
        "evidence": [
            {"id": str(e.id), "source_type": e.source_type, "source_ref": e.source_ref, "bureau": e.bureau,
             "description": e.description, "data": e.data}
            for e in claim.evidence
        ],
    }


def _extraction_incomplete_proposal(records: list[dict[str, Any]] | None = None) -> ClaimProposal:
    """A deterministic 'not enough was extracted to evaluate this account'
    result — never 'no dispute ground'. No AI call is made."""
    statuses = {
        (r.get("extraction_status") or ExtractionStatus.EXTRACTION_INCOMPLETE.value) for r in (records or [])
    }
    if ExtractionStatus.NEEDS_AUDIT.value in statuses:
        # The document was read fine — the verification pass just disagreed.
        # Telling the consumer to re-upload here would be wrong and useless.
        reasoning = (
            "This report was read successfully, but the verification pass found unresolved extraction "
            "differences. Dispute analysis is paused until those differences are reconciled. This is not a "
            "finding that the account is reported accurately."
        )
        needed = ["Reconciliation of the differences the verification pass flagged for this report."]
    else:
        reasoning = (
            "No dispute was evaluated because this account's report couldn't be read completely, so there "
            "isn't enough verified information to judge it. This is an extraction problem, not a finding "
            "that the account is reported accurately. Re-upload the report (a text-based PDF) and try again."
        )
        needed = ["A complete, readable copy of this account's tradeline from the report."]
    return ClaimProposal(
        has_dispute_ground=False,
        recommended_action="need_more_evidence",
        reasoning=reasoning,
        supporting_findings=[],
        disputed_fields=[],
        recipients=[],
        legal_explanations={},
        requested_remedy=None,
        additional_evidence_needed=needed,
        confidence=0.0,
        tier=ModelTier.REASONING,
        model="extraction_incomplete",
        validation_notes=["extraction_incomplete"],
    )


async def _current_claims(db: AsyncSession, canonical_ids: list) -> dict:
    if not canonical_ids:
        return {}
    result = await db.execute(
        select(Claim)
        .where(Claim.canonical_account_id.in_(canonical_ids), Claim.status == "current")
        .options(selectinload(Claim.evidence))
    )
    return {c.canonical_account_id: c for c in result.scalars().all()}


@router.get("/", response_model=list[dict[str, Any]])
async def list_accounts(user_id: uuid.UUID = Depends(current_user_id), db: AsyncSession = Depends(get_db)):
    views = await load_profile(db, user_id)
    claims = await _current_claims(db, [v.canonical.id for v in views])
    await db.commit()
    return [
        {**view_to_dict(v), "evaluation": claim_to_dict(claims[v.canonical.id]) if v.canonical.id in claims else None}
        for v in views
    ]


async def _owned_account(db: AsyncSession, account_id: str, user_id: uuid.UUID) -> AccountView:
    """Load a canonical account only if it belongs to the caller. Another
    user's account (or a nonexistent one) both return 404, so ownership is
    never disclosed."""
    view = await load_account(db, parse_uuid(account_id, "account_id"))
    if view is None or view.canonical.user_id != user_id:
        raise HTTPException(status_code=404, detail="Account not found")
    return view


@router.get("/{account_id}", response_model=dict[str, Any])
async def get_account(
    account_id: str, user_id: uuid.UUID = Depends(current_user_id), db: AsyncSession = Depends(get_db)
):
    view = await _owned_account(db, account_id, user_id)
    claims = await _current_claims(db, [view.canonical.id])
    cases = (await db.execute(
        select(Case).where(Case.canonical_account_id == view.canonical.id).order_by(Case.created_at.desc())
    )).scalars().all()
    return {
        **view_to_dict(view),
        "evaluation": claim_to_dict(claims[view.canonical.id]) if view.canonical.id in claims else None,
        "cases": [
            {"id": str(c.id), "status": c.status, "recipient_type": c.recipient_type, "recipient_name": c.recipient_name}
            for c in cases
        ],
    }


@router.post("/{account_id}/evaluate", response_model=dict[str, Any])
async def evaluate(
    account_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Run the reasoning engine on this one account and store the result as
    its current evaluation — a grounded dispute basis or an explicit
    'no dispute ground'. Opens nothing; the consumer decides on cases."""
    view = await _owned_account(db, account_id, user.id)

    # Precondition: only reason over records that were both read completely
    # AND verified against the original document. Returning "no dispute
    # ground" from missing or unverified evidence would be wrong (and would
    # spend an AI call); surface extraction-incomplete / need-more-evidence.
    usable = [
        r for r in view.records
        if record_is_disputable(r) and r.get("extraction_status") == ExtractionStatus.VERIFIED.value
    ]
    if not usable:
        proposal = _extraction_incomplete_proposal(view.records)
    else:
        # Identity is used only to build the redaction set (never inserted into
        # the prompt); it is the authenticated owner's own profile.
        identity = Identity.from_sources(None, user)
        proposal = await evaluate_account(
            view, context={"user_id": str(user.id), "canonical_account_id": account_id}, identity=identity,
        )
    claim = await save_evaluation(db, user.id, view.canonical.id, proposal)
    await db.commit()
    result = await db.execute(select(Claim).where(Claim.id == claim.id).options(selectinload(Claim.evidence)))
    return {**claim_to_dict(result.scalar_one()), "validation_notes": proposal.validation_notes}
