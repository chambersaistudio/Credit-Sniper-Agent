"""
Dispute management endpoints — create, approve, track, and update disputes.
"""
import uuid
import logging
from typing import Any
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.dispute import Dispute, DisputeLetter, DisputeRound
from app.models.credit_report import CreditAccount, CreditReport
from app.models.user import User
from app.services.dispute_tracker import (
    create_dispute,
    record_dispute_response,
    get_disputes_needing_action,
    check_for_no_response_disputes,
    is_safe_to_send_next_round,
)
from app.services.letter_generator import generate_letter, select_letter_type

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/disputes", tags=["disputes"])


class CreateDisputeRequest(BaseModel):
    account_id: str
    bureau: str
    dispute_type: str = "bureau_dispute"  # bureau_dispute | furnisher_direct | both
    furnisher_name: str | None = None
    furnisher_address: str | None = None
    user_id: str = "default"


class ApproveDisputeRequest(BaseModel):
    user_id: str = "default"


class RecordResponseRequest(BaseModel):
    response_type: str  # removed | updated | verified | denied | no_response | frivolous
    response_details: str
    raw_response: str | None = None


@router.post("/", response_model=dict[str, Any])
async def create_new_dispute(
    request: CreateDisputeRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new dispute for an account. Generates the optimal letter immediately.
    Status starts as pending_approval — user must approve before submission.
    """
    account_result = await db.execute(
        select(CreditAccount)
        .where(CreditAccount.id == uuid.UUID(request.account_id))
        .options(selectinload(CreditAccount.report))
    )
    account = account_result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    if not account.is_disputable:
        raise HTTPException(status_code=400, detail="Account is not marked as disputable")

    # Get user info for letter personalization
    user_info = await _get_user_info(db, request.user_id)

    # Determine letter type based on analysis
    analysis_item = account.raw_data or {}
    letter_type = select_letter_type(analysis_item, round_number=1)

    dispute = await create_dispute(
        db=db,
        user_id=uuid.UUID(request.user_id) if request.user_id != "default" else uuid.uuid4(),
        account_id=uuid.UUID(request.account_id),
        bureau=request.bureau,
        strategy=letter_type,
        dispute_type=request.dispute_type,
        furnisher_name=request.furnisher_name,
        furnisher_address=request.furnisher_address,
        priority=account.priority_score or 5,
        score_impact_estimate=analysis_item.get("estimated_score_impact", 0),
    )

    # Generate the letter
    try:
        letter_data = generate_letter(
            letter_type=letter_type,
            user_info=user_info,
            account_data={
                "creditor_name": account.creditor_name,
                "account_number": account.account_number,
                "account_type": account.account_type,
                "account_status": account.account_status,
                "balance": account.balance,
                "date_opened": account.date_opened,
                "date_of_first_delinquency": account.date_of_first_delinquency,
                "date_last_reported": account.date_last_reported,
                "payment_status": account.payment_status,
                "remarks": account.remarks,
            },
            analysis=analysis_item,
            bureau=request.bureau,
            round_number=1,
        )
    except Exception as e:
        logger.error(f"Letter generation failed: {e}")
        letter_data = {
            "subject": f"RE: Dispute of Account — {account.creditor_name}",
            "body": f"Letter generation failed: {e}. Please review account manually.",
            "recipient": request.bureau,
            "recipient_type": "bureau",
            "letter_type": letter_type,
            "legal_citations": [],
            "key_demands": [],
            "response_deadline_days": 30,
            "certified_mail_recommended": True,
        }

    letter = DisputeLetter(
        dispute_id=dispute.id,
        round_number=1,
        recipient=letter_data.get("recipient", request.bureau),
        recipient_type=letter_data.get("recipient_type", "bureau"),
        letter_type=letter_data.get("letter_type", letter_type),
        subject=letter_data.get("subject", ""),
        body=letter_data.get("body", ""),
        status="draft",
        legal_citations=letter_data.get("legal_citations", []),
    )
    db.add(letter)
    await db.commit()

    return {
        "dispute_id": str(dispute.id),
        "letter_id": str(letter.id),
        "status": dispute.status,
        "letter_type": letter_type,
        "letter_subject": letter.subject,
        "letter_body": letter.body,
        "legal_citations": letter_data.get("legal_citations", []),
        "key_demands": letter_data.get("key_demands", []),
        "certified_mail_recommended": letter_data.get("certified_mail_recommended", True),
        "next_action_date": str(dispute.next_action_date),
        "message": "Dispute created and letter drafted. Review and approve to proceed.",
    }


@router.post("/{dispute_id}/approve", response_model=dict[str, Any])
async def approve_dispute(
    dispute_id: str,
    request: ApproveDisputeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Approve a dispute letter for submission."""
    result = await db.execute(
        select(Dispute)
        .where(Dispute.id == uuid.UUID(dispute_id))
        .options(selectinload(Dispute.letters))
    )
    dispute = result.scalar_one_or_none()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")

    if dispute.status != "pending_approval":
        raise HTTPException(status_code=400, detail=f"Dispute is in '{dispute.status}' state, not pending_approval")

    dispute.status = "approved"
    for letter in dispute.letters:
        if letter.status == "draft":
            letter.status = "approved"

    await db.commit()

    return {
        "dispute_id": dispute_id,
        "status": "approved",
        "message": "Dispute approved. Ready for submission. Use the submit endpoint to send the letter.",
    }


@router.post("/{dispute_id}/submit", response_model=dict[str, Any])
async def submit_dispute(
    dispute_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Mark dispute as submitted (for manual submission workflow in Phase 1)."""
    result = await db.execute(
        select(Dispute)
        .where(Dispute.id == uuid.UUID(dispute_id))
        .options(selectinload(Dispute.letters))
    )
    dispute = result.scalar_one_or_none()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")

    if dispute.status != "approved":
        raise HTTPException(status_code=400, detail="Dispute must be approved before submission")

    now = datetime.now(timezone.utc)
    dispute.status = "submitted"
    for letter in dispute.letters:
        if letter.status == "approved":
            letter.status = "submitted"
            letter.submitted_at = now

    round_record = DisputeRound(
        dispute_id=dispute.id,
        round_number=dispute.current_round,
        sent_date=now,
    )
    db.add(round_record)
    await db.commit()

    return {
        "dispute_id": dispute_id,
        "status": "submitted",
        "submitted_at": str(now),
        "message": "Dispute marked as submitted. Monitor for response within 30 days.",
        "reminder": "Bureau has 30 days to reinvestigate (15 U.S.C. § 1681i). "
                    "If no response in 35 days, return to request mandatory deletion.",
    }


@router.post("/{dispute_id}/response", response_model=dict[str, Any])
async def record_response(
    dispute_id: str,
    request: RecordResponseRequest,
    db: AsyncSession = Depends(get_db),
):
    """Record the bureau/furnisher response and get next recommended action."""
    result = await db.execute(
        select(Dispute).where(Dispute.id == uuid.UUID(dispute_id))
    )
    dispute = result.scalar_one_or_none()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")

    next_step = await record_dispute_response(
        db=db,
        dispute_id=uuid.UUID(dispute_id),
        response_type=request.response_type,
        response_details=request.response_details,
        raw_response=request.raw_response,
    )

    return {
        "dispute_id": dispute_id,
        "response_recorded": request.response_type,
        "next_step": next_step,
    }


@router.get("/", response_model=list[dict[str, Any]])
async def list_disputes(db: AsyncSession = Depends(get_db)):
    """List all disputes with current status."""
    result = await db.execute(
        select(Dispute)
        .options(
            selectinload(Dispute.account),
            selectinload(Dispute.letters),
            selectinload(Dispute.rounds),
        )
        .order_by(Dispute.created_at.desc())
    )
    disputes = result.scalars().all()

    return [_format_dispute(d) for d in disputes]


@router.get("/pending-action", response_model=list[dict[str, Any]])
async def get_disputes_needing_attention(db: AsyncSession = Depends(get_db)):
    """Get all disputes requiring action (past due date, no response, etc.)."""
    overdue = await get_disputes_needing_action(db)
    no_response = await check_for_no_response_disputes(db)

    all_disputes = {str(d.id): d for d in overdue + no_response}

    return [
        {
            **_format_dispute(d),
            "action_urgency": "high" if d in no_response else "normal",
        }
        for d in all_disputes.values()
    ]


@router.get("/{dispute_id}", response_model=dict[str, Any])
async def get_dispute(dispute_id: str, db: AsyncSession = Depends(get_db)):
    """Get full dispute details including all letters and rounds."""
    result = await db.execute(
        select(Dispute)
        .where(Dispute.id == uuid.UUID(dispute_id))
        .options(
            selectinload(Dispute.account),
            selectinload(Dispute.letters),
            selectinload(Dispute.rounds),
        )
    )
    dispute = result.scalar_one_or_none()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")

    formatted = _format_dispute(dispute)
    formatted["letters"] = [
        {
            "id": str(l.id),
            "round_number": l.round_number,
            "letter_type": l.letter_type,
            "recipient": l.recipient,
            "subject": l.subject,
            "body": l.body,
            "status": l.status,
            "submitted_at": str(l.submitted_at) if l.submitted_at else None,
            "legal_citations": l.legal_citations,
        }
        for l in dispute.letters
    ]
    formatted["rounds"] = [
        {
            "round_number": r.round_number,
            "sent_date": str(r.sent_date) if r.sent_date else None,
            "response_received_date": str(r.response_received_date) if r.response_received_date else None,
            "response_type": r.response_type,
            "response_details": r.response_details,
            "next_strategy": r.next_strategy,
            "notes": r.notes,
        }
        for r in dispute.rounds
    ]

    return formatted


def _format_dispute(dispute: Dispute) -> dict[str, Any]:
    account = dispute.account
    return {
        "id": str(dispute.id),
        "bureau": dispute.bureau,
        "creditor_name": account.creditor_name if account else None,
        "account_number": account.account_number if account else None,
        "current_round": dispute.current_round,
        "status": dispute.status,
        "dispute_type": dispute.dispute_type,
        "strategy": dispute.strategy,
        "priority": dispute.priority,
        "score_impact_estimate": dispute.score_impact_estimate,
        "next_action_date": str(dispute.next_action_date) if dispute.next_action_date else None,
        "outcome": dispute.outcome,
        "created_at": str(dispute.created_at),
        "furnisher_name": dispute.furnisher_name,
    }


async def _get_user_info(db: AsyncSession, user_id: str) -> dict[str, Any]:
    """Fetch user info for letter personalization."""
    if user_id == "default":
        return {
            "full_name": "[YOUR FULL NAME]",
            "address": "[YOUR ADDRESS]",
            "city": "[CITY]",
            "state": "[STATE]",
            "zip_code": "[ZIP]",
            "ssn_last_four": "XXXX",
            "date_of_birth": "[DATE OF BIRTH]",
        }
    try:
        result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
        user = result.scalar_one_or_none()
        if user:
            return {
                "full_name": user.full_name,
                "address": user.address,
                "city": user.city,
                "state": user.state,
                "zip_code": user.zip_code,
                "ssn_last_four": user.ssn_last_four,
                "date_of_birth": user.date_of_birth,
            }
    except Exception:
        pass
    return {"full_name": "[YOUR FULL NAME]", "address": "[YOUR ADDRESS]"}
