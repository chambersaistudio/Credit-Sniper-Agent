"""
Read-only view of a user's cross-bureau canonical accounts: which bureau
records got linked together, at what confidence, and what the comparison
engine found between them. This is the first visible surface for the
Phase 1.5 account-matching + comparison-engine work — the evidence/case
API that acts on these findings comes in a later stage.
"""
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.canonical_account import CanonicalAccount, AccountLink
from app.services.comparison_engine import compare_canonical_account

router = APIRouter(prefix="/api/canonical-accounts", tags=["canonical-accounts"])


@router.get("/", response_model=list[dict[str, Any]])
async def list_canonical_accounts(user_id: str, db: AsyncSession = Depends(get_db)):
    """
    One entry per canonical account for the user, each with its linked
    per-bureau records and any cross-bureau comparison findings.
    """
    result = await db.execute(
        select(CanonicalAccount)
        .where(CanonicalAccount.user_id == uuid.UUID(user_id))
        .options(selectinload(CanonicalAccount.links).selectinload(AccountLink.credit_account))
    )
    canonical_accounts = result.scalars().all()

    output = []
    for canonical in canonical_accounts:
        bureau_records = []
        links_out = []
        for link in canonical.links:
            account = link.credit_account
            bureau_records.append(
                {
                    "bureau": link.bureau,
                    "balance": account.balance,
                    "account_status": account.account_status,
                    "payment_status": account.payment_status,
                    "date_of_first_delinquency": account.date_of_first_delinquency,
                }
            )
            links_out.append(
                {
                    "bureau": link.bureau,
                    "credit_account_id": str(account.id),
                    "confidence": link.confidence,
                    "matched_fields": link.matched_fields,
                    "account_number": account.account_number,
                    "account_status": account.account_status,
                    "balance": account.balance,
                }
            )

        findings = compare_canonical_account(bureau_records)
        output.append(
            {
                "id": str(canonical.id),
                "creditor_name": canonical.creditor_name,
                "account_type": canonical.account_type,
                "account_number_last_four": canonical.account_number_last_four,
                "bureaus_reporting": [l["bureau"] for l in links_out],
                "links": links_out,
                "comparison_findings": [
                    {
                        "field": f.field,
                        "severity": f.severity.value,
                        "values_by_bureau": f.values_by_bureau,
                        "rationale": f.rationale,
                    }
                    for f in findings
                ],
            }
        )
    return output
