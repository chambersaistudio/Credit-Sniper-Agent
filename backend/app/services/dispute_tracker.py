"""
Dispute tracking and strategic timing engine.
Enforces FCRA timing rules, manages escalation, and tracks all dispute outcomes.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.models.dispute import Dispute, DisputeLetter, DisputeRound
from app.models.credit_report import CreditAccount

logger = logging.getLogger(__name__)

# FCRA timing constants
BUREAU_REINVESTIGATION_DAYS = 30       # 15 U.S.C. § 1681i(a)(1)
BUREAU_MAX_REINVESTIGATION_DAYS = 45   # Extended if consumer provides additional info
MINIMUM_ROUND_SPACING_DAYS = 40        # Safe buffer after 30-day deadline
NO_RESPONSE_DELETION_DAYS = 35         # Request deletion if no response after 35 days


async def create_dispute(
    db: AsyncSession,
    user_id: UUID,
    account_id: UUID,
    bureau: str,
    strategy: str,
    dispute_type: str,
    furnisher_name: str | None = None,
    furnisher_address: str | None = None,
    priority: int = 5,
    score_impact_estimate: int = 0,
) -> Dispute:
    """Create a new dispute record."""
    now = datetime.now(timezone.utc)
    next_action = now + timedelta(days=BUREAU_REINVESTIGATION_DAYS + 5)

    dispute = Dispute(
        user_id=user_id,
        account_id=account_id,
        bureau=bureau,
        furnisher_name=furnisher_name,
        furnisher_address=furnisher_address,
        current_round=1,
        status="pending_approval",
        dispute_type=dispute_type,
        strategy=strategy,
        priority=priority,
        score_impact_estimate=score_impact_estimate,
        next_action_date=next_action,
    )
    db.add(dispute)
    await db.flush()
    return dispute


async def record_letter_submission(
    db: AsyncSession,
    letter_id: UUID,
    submission_method: str,
    tracking_number: str | None = None,
) -> None:
    """Record that a letter has been submitted."""
    now = datetime.now(timezone.utc)
    await db.execute(
        update(DisputeLetter)
        .where(DisputeLetter.id == letter_id)
        .values(
            status="submitted",
            submission_method=submission_method,
            submitted_at=now,
            certified_mail_tracking=tracking_number,
        )
    )

    # Update parent dispute
    result = await db.execute(
        select(DisputeLetter).where(DisputeLetter.id == letter_id)
    )
    letter = result.scalar_one_or_none()
    if letter:
        await db.execute(
            update(Dispute)
            .where(Dispute.id == letter.dispute_id)
            .values(status="submitted", next_action_date=now + timedelta(days=BUREAU_REINVESTIGATION_DAYS + 5))
        )

    await db.commit()


async def record_dispute_response(
    db: AsyncSession,
    dispute_id: UUID,
    response_type: str,
    response_details: str,
    raw_response: str | None = None,
) -> dict[str, Any]:
    """
    Record bureau/furnisher response and determine next action.
    Returns recommended next step.
    """
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(Dispute)
        .where(Dispute.id == dispute_id)
        .options(selectinload(Dispute.rounds))
    )
    dispute = result.scalar_one_or_none()
    if not dispute:
        raise ValueError(f"Dispute {dispute_id} not found")

    round_record = DisputeRound(
        dispute_id=dispute_id,
        round_number=dispute.current_round,
        response_received_date=now,
        response_type=response_type,
        response_details=response_details,
        bureau_response_raw=raw_response,
    )

    next_step = _determine_next_step(response_type, dispute.current_round)
    round_record.next_strategy = next_step["strategy"]
    round_record.notes = next_step["notes"]
    db.add(round_record)

    # Update dispute status
    new_status = _map_response_to_status(response_type)
    new_round = dispute.current_round + 1 if response_type not in ("removed", "frivolous") else dispute.current_round
    next_action = now + timedelta(days=MINIMUM_ROUND_SPACING_DAYS) if response_type not in ("removed",) else None

    await db.execute(
        update(Dispute)
        .where(Dispute.id == dispute_id)
        .values(
            status=new_status,
            current_round=new_round,
            last_response_date=now,
            next_action_date=next_action,
            outcome=response_type if response_type in ("removed", "verified", "denied") else None,
        )
    )

    await db.commit()
    return next_step


def _determine_next_step(response_type: str, current_round: int) -> dict[str, Any]:
    """Determine the optimal next step based on bureau response."""
    if response_type == "removed":
        return {
            "strategy": "monitor",
            "notes": "SUCCESS: Item removed. Monitor for 90 days to ensure no re-insertion. "
                     "If re-inserted within 5 business days, bureau must notify you — file immediate re-dispute.",
            "action_required": False,
        }

    if response_type == "no_response":
        return {
            "strategy": "deletion_demand",
            "notes": f"Bureau failed to respond within {BUREAU_REINVESTIGATION_DAYS} days. "
                     "Under 15 U.S.C. § 1681i(a)(5)(A), they must delete the item. "
                     "Send immediate deletion demand citing failure to reinvestigate.",
            "action_required": True,
            "urgent": True,
        }

    if response_type == "verified":
        if current_round == 1:
            return {
                "strategy": "section_609_method_of_verification",
                "notes": "Bureau verified but we need to attack the METHOD of verification. "
                         "Request exactly how they verified: what documentation did the furnisher provide? "
                         "e-OSCAR automated verifications are often insufficient under FCRA.",
                "action_required": True,
            }
        elif current_round == 2:
            return {
                "strategy": "furnisher_direct_dispute",
                "notes": "Bureau hiding behind furnisher verification. Attack furnisher directly under "
                         "15 U.S.C. § 1681s-2(a)(8). Simultaneously send FCRA violation letter to bureau "
                         "citing inadequate reinvestigation procedures.",
                "action_required": True,
            }
        else:
            return {
                "strategy": "legal_escalation",
                "notes": f"Round {current_round}: Multiple verifications without adequate investigation. "
                         "Document pattern for potential FCRA lawsuit. Consider demand letter with "
                         "notice of litigation intent. Damages: $100-$1,000 per willful violation + punitive.",
                "action_required": True,
            }

    if response_type == "updated":
        return {
            "strategy": "verify_update_and_escalate_if_incomplete",
            "notes": "Item was updated but NOT deleted. Pull new report to verify the update is accurate "
                     "and favorable. If update is insufficient, dispute the remaining inaccuracies.",
            "action_required": True,
        }

    if response_type == "denied":
        return {
            "strategy": "new_angle_attack",
            "notes": "Bureau denied dispute. Reframe with completely different legal angle. "
                     "Do NOT reuse same arguments — that risks frivolous flag. "
                     "If denied multiple times, consider CFPB complaint + state attorney general.",
            "action_required": True,
        }

    return {
        "strategy": "review_required",
        "notes": "Unusual response received. Manual review required.",
        "action_required": True,
    }


def _map_response_to_status(response_type: str) -> str:
    mapping = {
        "removed": "resolved",
        "updated": "response_received",
        "verified": "response_received",
        "denied": "response_received",
        "no_response": "escalated",
        "frivolous": "frivolous_flagged",
    }
    return mapping.get(response_type, "response_received")


async def get_disputes_needing_action(db: AsyncSession) -> list[Dispute]:
    """Return disputes whose next_action_date has passed."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Dispute)
        .where(
            Dispute.next_action_date <= now,
            Dispute.status.in_(["submitted", "response_received"]),
        )
        .options(selectinload(Dispute.account), selectinload(Dispute.rounds))
    )
    return list(result.scalars().all())


async def check_for_no_response_disputes(db: AsyncSession) -> list[Dispute]:
    """Find disputes submitted 35+ days ago with no response — eligible for deletion demand."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=NO_RESPONSE_DELETION_DAYS)
    result = await db.execute(
        select(Dispute)
        .where(
            Dispute.status == "submitted",
            Dispute.updated_at <= cutoff,
        )
    )
    return list(result.scalars().all())


def calculate_next_round_date(last_submitted: datetime) -> datetime:
    """Calculate earliest safe date for next dispute round (40-day spacing)."""
    return last_submitted + timedelta(days=MINIMUM_ROUND_SPACING_DAYS)


def is_safe_to_send_next_round(last_submitted: datetime) -> bool:
    """Check if enough time has passed to safely send next round without frivolous flag risk."""
    return datetime.now(timezone.utc) >= calculate_next_round_date(last_submitted)
