"""
Cases: one dispute to one recipient, opened only from grounded claims.

Lifecycle actions map to explicit endpoints so each one can carry its own
policy (e.g. approval requires a ready package). Submission in Phase 1.5 is
a manual handoff: the consumer sends the package and records how and when.
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.accounts import claim_to_dict
from app.auth import current_user_id
from app.database import get_db
from app.models.canonical_account import AccountLink, CanonicalAccount
from app.models.case import Case, Claim
from app.models.user import User
from app.services.bureau_directory import BUREAUS
from app.services.case_service import add_event, transition
from app.services.case_state_machine import (
    AWAITING_RESPONSE, NEEDS_USER, OUTCOMES, CaseStatus, compute_response_due, is_overdue, next_action,
)
from app.services.dispute_package import build_package
from app.services.package_pdf import render_package_pdf
from app.services.storage import approved_package_key, get_storage
from app.utils.default_user import parse_uuid

router = APIRouter(prefix="/api/cases", tags=["cases"])


class OpenCaseRequest(BaseModel):
    claim_ids: list[str]
    recipient: str  # "equifax" | "experian" | "transunion" | "furnisher"
    furnisher_name: str | None = None
    furnisher_address: str | None = None


class SubmittedRequest(BaseModel):
    channel: Literal["manual_mail", "certified_mail", "portal", "email"]
    submitted_at: datetime | None = None
    tracking_number: str | None = None


class DeliveredRequest(BaseModel):
    delivered_at: datetime | None = None


class ResponseRequest(BaseModel):
    outcome: Literal["corrected", "deleted", "verified", "more_information_required"]
    received_at: datetime | None = None
    detail: str = ""


class TransitionRequest(BaseModel):
    target: Literal["draft", "monitoring", "followup_recommended", "resolved"]
    detail: str = ""


class FurnisherUpdate(BaseModel):
    furnisher_address: str


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _load_case(db: AsyncSession, case_id: str, user_id: uuid.UUID) -> Case:
    """Load a case only if it belongs to the caller. A case owned by someone
    else returns the same 404 as a nonexistent one — changing the id in the
    URL never reveals or reaches another user's case."""
    result = await db.execute(
        select(Case)
        .where(Case.id == parse_uuid(case_id, "case_id"), Case.user_id == user_id)
        .options(
            selectinload(Case.claims).selectinload(Claim.evidence),
            selectinload(Case.events),
            selectinload(Case.canonical_account),
        )
    )
    case = result.scalar_one_or_none()
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


def _case_to_dict(case: Case, detail: bool = False) -> dict[str, Any]:
    status = CaseStatus(case.status)
    out = {
        "id": str(case.id),
        "status": case.status,
        "canonical_account_id": str(case.canonical_account_id),
        "creditor_name": case.canonical_account.creditor_name if case.canonical_account else None,
        "recipient_type": case.recipient_type,
        "recipient_name": case.recipient_name,
        "recipient_display": BUREAUS[case.recipient_name].display_name if case.recipient_name in BUREAUS else case.recipient_name,
        "submission_channel": case.submission_channel,
        "tracking_number": case.tracking_number,
        "submitted_at": case.submitted_at.isoformat() if case.submitted_at else None,
        "delivered_at": case.delivered_at.isoformat() if case.delivered_at else None,
        "response_due_at": case.response_due_at.isoformat() if case.response_due_at else None,
        "deadline_basis": case.deadline_basis,
        "response_received_at": case.response_received_at.isoformat() if case.response_received_at else None,
        "outcome": case.outcome,
        "overdue": is_overdue(status, case.response_due_at),
        "needs_user": status in NEEDS_USER or is_overdue(status, case.response_due_at),
        "next_action": next_action(status, case.response_due_at),
        "has_package": case.package is not None,
        "created_at": case.created_at.isoformat() if case.created_at else None,
        "updated_at": case.updated_at.isoformat() if case.updated_at else None,
    }
    if detail:
        out["claims"] = [claim_to_dict(c) for c in case.claims]
        out["package"] = case.package
        out["events"] = [
            {"event_type": e.event_type, "from_status": e.from_status, "to_status": e.to_status, "actor": e.actor,
             "detail": e.detail, "data": e.data, "created_at": e.created_at.isoformat() if e.created_at else None}
            for e in case.events
        ]
    return out


@router.post("/", response_model=dict[str, Any])
async def open_case(
    request: OpenCaseRequest, user_id: uuid.UUID = Depends(current_user_id), db: AsyncSession = Depends(get_db)
):
    if not request.claim_ids:
        raise HTTPException(status_code=422, detail="A case needs at least one claim")
    claim_uuids = [parse_uuid(c, "claim_id") for c in request.claim_ids]
    # Scope the lookup to the caller's own claims: a claim id belonging to
    # someone else is simply not found, so a case can never be built on
    # another user's evaluation.
    claims = (
        await db.execute(select(Claim).where(Claim.id.in_(claim_uuids), Claim.user_id == user_id))
    ).scalars().all()
    if len(claims) != len(set(claim_uuids)):
        raise HTTPException(status_code=404, detail="Claim not found")

    accounts = {c.canonical_account_id for c in claims}
    if len(accounts) != 1:
        raise HTTPException(status_code=422, detail="All claims in a case must concern the same account")
    for claim in claims:
        if claim.status != "current":
            raise HTTPException(status_code=409, detail="This evaluation is out of date — re-evaluate the account first")
        if not claim.has_dispute_ground:
            raise HTTPException(status_code=422, detail="No dispute ground was identified for this account, so there's nothing to dispute")
        if request.recipient not in (claim.recipients or []):
            raise HTTPException(status_code=422, detail=f"The evaluation doesn't support disputing with {request.recipient}")

    canonical = await db.get(CanonicalAccount, accounts.pop())
    if request.recipient == "furnisher":
        recipient_type, recipient_name = "furnisher", (request.furnisher_name or canonical.creditor_name)
    elif request.recipient in BUREAUS:
        recipient_type, recipient_name = "bureau", request.recipient
    else:
        raise HTTPException(status_code=422, detail=f"Unknown recipient: {request.recipient}")

    open_statuses = [s.value for s in CaseStatus if s != CaseStatus.RESOLVED]
    duplicate = (await db.execute(
        select(Case.id).where(
            Case.canonical_account_id == canonical.id, Case.recipient_name == recipient_name,
            Case.status.in_(open_statuses),
        )
    )).first()
    if duplicate:
        raise HTTPException(status_code=409, detail=f"There's already an open case for this account with {recipient_name}")

    case = Case(
        user_id=user_id,
        canonical_account_id=canonical.id,
        recipient_type=recipient_type,
        recipient_name=recipient_name,
        recipient_address=request.furnisher_address if recipient_type == "furnisher" else None,
        status=CaseStatus.DRAFT.value,
        claims=list(claims),
    )
    db.add(case)
    await db.flush()
    add_event(db, case, "created", to_status=CaseStatus.DRAFT.value,
              detail=f"Opened from {len(claims)} claim(s)", data={"claim_ids": request.claim_ids})
    await db.commit()
    return _case_to_dict(await _load_case(db, str(case.id), user_id), detail=True)


@router.get("/", response_model=list[dict[str, Any]])
async def list_cases(user_id: uuid.UUID = Depends(current_user_id), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Case).where(Case.user_id == user_id)
        .options(selectinload(Case.canonical_account))
        .order_by(Case.updated_at.desc())
    )
    await db.commit()
    return [_case_to_dict(c) for c in result.scalars().all()]


@router.get("/{case_id}", response_model=dict[str, Any])
async def get_case(case_id: str, user_id: uuid.UUID = Depends(current_user_id), db: AsyncSession = Depends(get_db)):
    return _case_to_dict(await _load_case(db, case_id, user_id), detail=True)


@router.patch("/{case_id}/furnisher", response_model=dict[str, Any])
async def set_furnisher_address(
    case_id: str, request: FurnisherUpdate, user_id: uuid.UUID = Depends(current_user_id), db: AsyncSession = Depends(get_db)
):
    case = await _load_case(db, case_id, user_id)
    if case.recipient_type != "furnisher":
        raise HTTPException(status_code=422, detail="Only furnisher cases take an address")
    if CaseStatus(case.status) not in (CaseStatus.DRAFT, CaseStatus.AWAITING_APPROVAL):
        raise HTTPException(status_code=409, detail="The address can't change after approval")
    case.recipient_address = request.furnisher_address.strip()
    add_event(db, case, "note", detail="Furnisher dispute address updated")
    await db.commit()
    return _case_to_dict(await _load_case(db, case_id, user_id), detail=True)


@router.post("/{case_id}/package", response_model=dict[str, Any])
async def generate_package(case_id: str, user_id: uuid.UUID = Depends(current_user_id), db: AsyncSession = Depends(get_db)):
    """(Re)build the dispute package from the case's claims and evidence and
    move the case to awaiting approval."""
    case = await _load_case(db, case_id, user_id)
    status = CaseStatus(case.status)
    if status == CaseStatus.AWAITING_APPROVAL:
        transition(db, case, CaseStatus.DRAFT, detail="Package regenerated")
    elif status != CaseStatus.DRAFT:
        raise HTTPException(status_code=409, detail="Packages can only be (re)generated before approval")

    user = await db.get(User, case.user_id)
    links = (await db.execute(
        select(AccountLink).where(AccountLink.canonical_account_id == case.canonical_account_id)
        .options(selectinload(AccountLink.credit_account))
    )).scalars().all()
    # Use the recipient bureau's own masked number when it has one.
    preferred = [l for l in links if l.bureau == case.recipient_name] or links
    account_number = next((l.credit_account.account_number for l in preferred if l.credit_account.account_number), None)

    case.package = build_package(case, list(case.claims), user, case.canonical_account.creditor_name, account_number)
    case.package_generated_at = datetime.now(timezone.utc)
    add_event(db, case, "package_generated", actor="system", data={"ready": case.package["ready"], "warnings": case.package["warnings"]})
    transition(db, case, CaseStatus.AWAITING_APPROVAL, actor="system", detail="Package ready for review")
    await db.commit()
    return _case_to_dict(await _load_case(db, case_id, user_id), detail=True)


@router.get("/{case_id}/package.pdf")
async def download_package(case_id: str, user_id: uuid.UUID = Depends(current_user_id), db: AsyncSession = Depends(get_db)):
    case = await _load_case(db, case_id, user_id)
    if not case.package:
        raise HTTPException(status_code=404, detail="No package generated yet")
    pdf = render_package_pdf(case.package)
    filename = f"dispute-{case.recipient_name.lower().replace(' ', '-')}-{str(case.id)[:8]}.pdf"
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("/{case_id}/approve", response_model=dict[str, Any])
async def approve(case_id: str, user_id: uuid.UUID = Depends(current_user_id), db: AsyncSession = Depends(get_db)):
    case = await _load_case(db, case_id, user_id)
    if not case.package or not case.package.get("ready"):
        warnings = (case.package or {}).get("warnings") or ["Generate the package first."]
        raise HTTPException(status_code=409, detail="The package isn't ready to approve: " + " ".join(warnings))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    case.approved_package_key = approved_package_key(case.user_id, case.id, stamp)
    await get_storage().put(case.approved_package_key, render_package_pdf(case.package), "application/pdf")
    transition(db, case, CaseStatus.APPROVED, detail="Consumer approved the dispute package",
               data={"approved_package_key": case.approved_package_key})
    await db.commit()
    return _case_to_dict(await _load_case(db, case_id, user_id), detail=True)


@router.post("/{case_id}/submitted", response_model=dict[str, Any])
async def mark_submitted(case_id: str, request: SubmittedRequest, user_id: uuid.UUID = Depends(current_user_id), db: AsyncSession = Depends(get_db)):
    case = await _load_case(db, case_id, user_id)
    case.submitted_at = _utc(request.submitted_at)
    case.submission_channel = request.channel
    case.tracking_number = request.tracking_number
    case.response_due_at, case.deadline_basis = compute_response_due(case.submitted_at, None, case.extended_investigation)
    transition(db, case, CaseStatus.SUBMITTED, detail=f"Sent via {request.channel.replace('_', ' ')}",
               data={"tracking_number": request.tracking_number})
    await db.commit()
    return _case_to_dict(await _load_case(db, case_id, user_id), detail=True)


@router.post("/{case_id}/delivered", response_model=dict[str, Any])
async def mark_delivered(case_id: str, request: DeliveredRequest, user_id: uuid.UUID = Depends(current_user_id), db: AsyncSession = Depends(get_db)):
    case = await _load_case(db, case_id, user_id)
    case.delivered_at = _utc(request.delivered_at)
    case.response_due_at, case.deadline_basis = compute_response_due(case.submitted_at, case.delivered_at, case.extended_investigation)
    transition(db, case, CaseStatus.DELIVERED, detail="Delivery confirmed; investigation clock running from receipt")
    await db.commit()
    return _case_to_dict(await _load_case(db, case_id, user_id), detail=True)


@router.post("/{case_id}/response", response_model=dict[str, Any])
async def record_response(case_id: str, request: ResponseRequest, user_id: uuid.UUID = Depends(current_user_id), db: AsyncSession = Depends(get_db)):
    case = await _load_case(db, case_id, user_id)
    if CaseStatus(case.status) not in AWAITING_RESPONSE | {CaseStatus.RESPONSE_RECEIVED}:
        raise HTTPException(status_code=409, detail="Record a response only after the dispute was sent")
    case.response_received_at = _utc(request.received_at)
    case.outcome = request.outcome
    if CaseStatus(case.status) != CaseStatus.RESPONSE_RECEIVED:
        transition(db, case, CaseStatus.RESPONSE_RECEIVED, detail="Response received")
    transition(db, case, OUTCOMES[request.outcome], detail=request.detail or f"Outcome: {request.outcome.replace('_', ' ')}")
    await db.commit()
    return _case_to_dict(await _load_case(db, case_id, user_id), detail=True)


@router.post("/{case_id}/transition", response_model=dict[str, Any])
async def move(case_id: str, request: TransitionRequest, user_id: uuid.UUID = Depends(current_user_id), db: AsyncSession = Depends(get_db)):
    case = await _load_case(db, case_id, user_id)
    transition(db, case, CaseStatus(request.target), detail=request.detail or None)
    await db.commit()
    return _case_to_dict(await _load_case(db, case_id, user_id), detail=True)
