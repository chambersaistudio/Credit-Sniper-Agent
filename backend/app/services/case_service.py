"""
Case operations. Every status change goes through `transition`, which
validates against the state machine and appends an audit event — there is
no other code path that mutates Case.status.
"""
import uuid
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.case import Case, CaseEvent, Claim, Evidence
from app.services.case_state_machine import CaseStatus, validate_transition
from app.services.reasoning_engine import ClaimProposal


def add_event(
    db: AsyncSession, case: Case, event_type: str, *, actor: str = "user", detail: str | None = None,
    data: dict[str, Any] | None = None, from_status: str | None = None, to_status: str | None = None,
) -> CaseEvent:
    event = CaseEvent(
        case_id=case.id, event_type=event_type, actor=actor, detail=detail, data=data,
        from_status=from_status, to_status=to_status,
    )
    db.add(event)
    return event


def transition(
    db: AsyncSession, case: Case, target: CaseStatus, *, actor: str = "user",
    detail: str | None = None, data: dict[str, Any] | None = None,
) -> None:
    current = CaseStatus(case.status)
    validate_transition(current, target)
    case.status = target.value
    add_event(db, case, "status_change", actor=actor, detail=detail, data=data,
              from_status=current.value, to_status=target.value)


async def save_evaluation(
    db: AsyncSession, user_id: uuid.UUID, canonical_account_id: uuid.UUID, proposal: ClaimProposal
) -> Claim:
    """Persist a reasoning-engine proposal as the account's current claim,
    superseding earlier evaluations. Stored even when there's no dispute
    ground, so every decision has a recorded reason."""
    await db.execute(
        update(Claim)
        .where(Claim.canonical_account_id == canonical_account_id, Claim.status == "current")
        .values(status="superseded")
    )
    claim = Claim(
        user_id=user_id,
        canonical_account_id=canonical_account_id,
        has_dispute_ground=proposal.has_dispute_ground,
        recommended_action=proposal.recommended_action,
        reasoning=proposal.reasoning,
        disputed_fields=proposal.disputed_fields,
        recipients=proposal.recipients,
        legal_reference_ids=list(proposal.legal_explanations),
        legal_explanations=proposal.legal_explanations,
        requested_remedy=proposal.requested_remedy,
        additional_evidence_needed=proposal.additional_evidence_needed,
        confidence=proposal.confidence,
        model_tier=proposal.tier.value,
        model=proposal.model,
    )
    db.add(claim)
    await db.flush()
    for finding in proposal.supporting_findings:
        db.add(Evidence(
            claim_id=claim.id,
            source_type="finding",
            source_ref=finding.rule,
            bureau=finding.bureau,
            description=finding.rationale,
            data=finding.to_dict(),
        ))
    return claim
