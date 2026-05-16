"""
Letter generation and management endpoints.
"""
import uuid
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.dispute import Dispute, DisputeLetter
from app.models.credit_report import CreditAccount
from app.services.letter_generator import (
    generate_letter,
    generate_furnisher_letter,
    select_letter_type,
    LETTER_TYPE_DESCRIPTIONS,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/letters", tags=["letters"])


class RegenerateLetterRequest(BaseModel):
    letter_type: str | None = None
    custom_notes: str | None = None


class FurnisherLetterRequest(BaseModel):
    dispute_id: str
    furnisher_name: str
    furnisher_address: str


@router.get("/{letter_id}", response_model=dict[str, Any])
async def get_letter(letter_id: str, db: AsyncSession = Depends(get_db)):
    """Get a specific dispute letter."""
    result = await db.execute(
        select(DisputeLetter).where(DisputeLetter.id == uuid.UUID(letter_id))
    )
    letter = result.scalar_one_or_none()
    if not letter:
        raise HTTPException(status_code=404, detail="Letter not found")

    return _format_letter(letter)


@router.post("/{letter_id}/regenerate", response_model=dict[str, Any])
async def regenerate_letter(
    letter_id: str,
    request: RegenerateLetterRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Regenerate a letter with a different type or incorporating custom notes.
    Use this to try different approaches or add new evidence.
    """
    letter_result = await db.execute(
        select(DisputeLetter)
        .where(DisputeLetter.id == uuid.UUID(letter_id))
        .options(selectinload(DisputeLetter.dispute))
    )
    letter = letter_result.scalar_one_or_none()
    if not letter:
        raise HTTPException(status_code=404, detail="Letter not found")

    if letter.status in ("submitted", "delivered"):
        raise HTTPException(status_code=400, detail="Cannot regenerate a letter that has already been submitted")

    dispute = letter.dispute

    account_result = await db.execute(
        select(CreditAccount).where(CreditAccount.id == dispute.account_id)
    )
    account = account_result.scalar_one_or_none()

    analysis_item = account.raw_data or {} if account else {}
    if request.custom_notes:
        analysis_item["custom_notes"] = request.custom_notes

    letter_type = request.letter_type or select_letter_type(analysis_item, dispute.current_round)

    new_letter_data = generate_letter(
        letter_type=letter_type,
        user_info={"full_name": "[YOUR FULL NAME]", "address": "[YOUR ADDRESS]"},
        account_data={
            "creditor_name": account.creditor_name if account else "Unknown",
            "account_number": account.account_number if account else "Unknown",
            "account_status": account.account_status if account else "Unknown",
            "balance": account.balance if account else 0,
        },
        analysis=analysis_item,
        bureau=dispute.bureau,
        round_number=dispute.current_round,
    )

    letter.letter_type = letter_type
    letter.subject = new_letter_data.get("subject", letter.subject)
    letter.body = new_letter_data.get("body", letter.body)
    letter.legal_citations = new_letter_data.get("legal_citations", [])
    letter.status = "draft"

    await db.commit()

    return {
        **_format_letter(letter),
        "regenerated": True,
        "message": f"Letter regenerated as {letter_type}. Review and approve to proceed.",
    }


@router.post("/furnisher", response_model=dict[str, Any])
async def create_furnisher_letter(
    request: FurnisherLetterRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Generate a direct furnisher dispute letter under 15 U.S.C. § 1681s-2(a)(8).
    This is a separate letter sent directly to the company reporting the debt,
    not to the credit bureau.
    """
    dispute_result = await db.execute(
        select(Dispute)
        .where(Dispute.id == uuid.UUID(request.dispute_id))
        .options(selectinload(Dispute.account))
    )
    dispute = dispute_result.scalar_one_or_none()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")

    account = dispute.account
    analysis_item = account.raw_data or {} if account else {}

    letter_data = generate_furnisher_letter(
        user_info={"full_name": "[YOUR FULL NAME]", "address": "[YOUR ADDRESS]"},
        account_data={
            "creditor_name": account.creditor_name if account else request.furnisher_name,
            "account_number": account.account_number if account else "Unknown",
            "account_status": account.account_status if account else "Unknown",
            "balance": account.balance if account else 0,
        },
        analysis=analysis_item,
        furnisher_name=request.furnisher_name,
        furnisher_address=request.furnisher_address,
        round_number=dispute.current_round,
    )

    letter = DisputeLetter(
        dispute_id=dispute.id,
        round_number=dispute.current_round,
        recipient=request.furnisher_name,
        recipient_type="furnisher",
        letter_type="furnisher_direct",
        subject=letter_data.get("subject", ""),
        body=letter_data.get("body", ""),
        status="draft",
        legal_citations=letter_data.get("legal_citations", []),
    )
    db.add(letter)
    await db.commit()

    return {
        **_format_letter(letter),
        "message": "Furnisher direct dispute letter created. This should be sent via certified mail to the furnisher.",
    }


@router.get("/types/list", response_model=dict[str, str])
async def list_letter_types():
    """List all available letter types with descriptions."""
    return LETTER_TYPE_DESCRIPTIONS


def _format_letter(letter: DisputeLetter) -> dict[str, Any]:
    return {
        "id": str(letter.id),
        "dispute_id": str(letter.dispute_id),
        "round_number": letter.round_number,
        "letter_type": letter.letter_type,
        "recipient": letter.recipient,
        "recipient_type": letter.recipient_type,
        "subject": letter.subject,
        "body": letter.body,
        "status": letter.status,
        "submission_method": letter.submission_method,
        "submitted_at": str(letter.submitted_at) if letter.submitted_at else None,
        "certified_mail_tracking": letter.certified_mail_tracking,
        "legal_citations": letter.legal_citations,
        "created_at": str(letter.created_at),
    }
