"""Home and Activity screens: what the profile looks like and what's waiting on the consumer."""
from collections import Counter
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.case import Case, CaseEvent, Claim
from app.models.credit_report import CreditReport
from app.services.case_state_machine import AWAITING_RESPONSE, NEEDS_USER, CaseStatus, is_overdue, next_action
from app.services.credit_profile import load_profile
from app.utils.default_user import resolve_user_id

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/dashboard", response_model=dict[str, Any])
async def dashboard(user_id: str = "default", db: AsyncSession = Depends(get_db)):
    resolved = await resolve_user_id(db, user_id)

    reports = (await db.execute(
        select(CreditReport).where(CreditReport.user_id == resolved).order_by(CreditReport.pull_date.desc())
    )).scalars().all()
    scores: dict[str, dict[str, Any]] = {}
    for report in reports:
        if report.bureau not in scores:
            scores[report.bureau] = {
                "score": report.credit_score,
                "report_date": (report.report_date or report.pull_date).date().isoformat(),
            }

    views = await load_profile(db, resolved)
    claims = (await db.execute(
        select(Claim).where(Claim.user_id == resolved, Claim.status == "current")
    )).scalars().all()
    cases = (await db.execute(
        select(Case).where(Case.user_id == resolved).options(selectinload(Case.canonical_account))
    )).scalars().all()
    await db.commit()

    severities = Counter(f.severity.value for v in views for f in v.findings)
    needs_user = [
        c for c in cases
        if CaseStatus(c.status) in NEEDS_USER or is_overdue(CaseStatus(c.status), c.response_due_at)
    ]
    return {
        "has_reports": bool(reports),
        "scores": scores,
        "accounts": {
            "total": len(views),
            "negative": sum(v.is_negative for v in views),
            "with_findings": sum(bool(v.findings) for v in views),
            "evaluated": len(claims),
            "dispute_ground": sum(c.has_dispute_ground for c in claims),
        },
        "findings_by_severity": dict(severities),
        "cases": {
            "open": sum(c.status != CaseStatus.RESOLVED.value for c in cases),
            "needs_user": len(needs_user),
            "awaiting_response": sum(CaseStatus(c.status) in AWAITING_RESPONSE for c in cases),
            "overdue": sum(is_overdue(CaseStatus(c.status), c.response_due_at) for c in cases),
            "resolved": sum(c.status == CaseStatus.RESOLVED.value for c in cases),
        },
        "waiting_on_you": [
            {
                "case_id": str(c.id),
                "creditor_name": c.canonical_account.creditor_name,
                "recipient_name": c.recipient_name,
                "status": c.status,
                "next_action": next_action(CaseStatus(c.status), c.response_due_at),
            }
            for c in sorted(needs_user, key=lambda c: c.updated_at, reverse=True)
        ],
    }


@router.get("/activity", response_model=list[dict[str, Any]])
async def activity(user_id: str = "default", limit: int = 50, db: AsyncSession = Depends(get_db)):
    resolved = await resolve_user_id(db, user_id)
    limit = max(1, min(limit, 200))
    events = (await db.execute(
        select(CaseEvent, Case)
        .join(Case, Case.id == CaseEvent.case_id)
        .where(Case.user_id == resolved)
        .options(selectinload(Case.canonical_account))
        .order_by(CaseEvent.created_at.desc())
        .limit(limit)
    )).all()
    reports = (await db.execute(
        select(CreditReport).where(CreditReport.user_id == resolved).order_by(CreditReport.created_at.desc()).limit(limit)
    )).scalars().all()
    await db.commit()

    items = [
        {
            "type": "case_event",
            "case_id": str(case.id),
            "creditor_name": case.canonical_account.creditor_name,
            "recipient_name": case.recipient_name,
            "event_type": event.event_type,
            "from_status": event.from_status,
            "to_status": event.to_status,
            "actor": event.actor,
            "detail": event.detail,
            "at": event.created_at.isoformat(),
        }
        for event, case in events
    ] + [
        {"type": "report_uploaded", "report_id": str(r.id), "bureau": r.bureau, "at": r.created_at.isoformat()}
        for r in reports
    ]
    return sorted(items, key=lambda i: i["at"], reverse=True)[:limit]
