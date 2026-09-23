"""
The consumer's normalized credit profile: one entry per real-world account,
its current record per bureau, deterministic findings, the latest
evaluation (claim), and any cases.
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.case import Case, Claim
from app.services.case_service import save_evaluation
from app.services.credit_profile import load_account, load_profile, view_to_dict
from app.services.legal_references import REFERENCES
from app.services.reasoning_engine import evaluate_account
from app.utils.default_user import parse_uuid, resolve_user_id

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
async def list_accounts(user_id: str = "default", db: AsyncSession = Depends(get_db)):
    resolved = await resolve_user_id(db, user_id)
    views = await load_profile(db, resolved)
    claims = await _current_claims(db, [v.canonical.id for v in views])
    await db.commit()
    return [
        {**view_to_dict(v), "evaluation": claim_to_dict(claims[v.canonical.id]) if v.canonical.id in claims else None}
        for v in views
    ]


@router.get("/{account_id}", response_model=dict[str, Any])
async def get_account(account_id: str, db: AsyncSession = Depends(get_db)):
    view = await load_account(db, parse_uuid(account_id, "account_id"))
    if view is None:
        raise HTTPException(status_code=404, detail="Account not found")
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
async def evaluate(account_id: str, db: AsyncSession = Depends(get_db)):
    """Run the reasoning engine on this one account and store the result as
    its current evaluation — a grounded dispute basis or an explicit
    'no dispute ground'. Opens nothing; the consumer decides on cases."""
    view = await load_account(db, parse_uuid(account_id, "account_id"))
    if view is None:
        raise HTTPException(status_code=404, detail="Account not found")
    user_id = view.canonical.user_id
    proposal = await evaluate_account(view, context={"user_id": str(user_id), "canonical_account_id": account_id})
    claim = await save_evaluation(db, user_id, view.canonical.id, proposal)
    await db.commit()
    result = await db.execute(select(Claim).where(Claim.id == claim.id).options(selectinload(Claim.evidence)))
    return {**claim_to_dict(result.scalar_one()), "validation_notes": proposal.validation_notes}
