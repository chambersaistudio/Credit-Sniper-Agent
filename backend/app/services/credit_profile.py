"""
The normalized credit profile: each canonical account with its current
record per bureau and every deterministic finding about it. This is the
structured source of truth that UI, cases, and the reasoning engine read —
the AI only ever sees the slice for the account it's reasoning about.
"""
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.canonical_account import AccountLink, CanonicalAccount
from app.models.credit_report import CreditAccount
from app.services.account_rules import evaluate_record, is_negative
from app.services.comparison_engine import compare_bureau_records
from app.services.findings import Finding, sort_by_severity

RECORD_FIELDS = (
    "creditor_name", "account_number", "account_type", "account_status", "payment_status",
    "balance", "past_due_amount", "high_balance", "credit_limit", "original_amount", "monthly_payment",
    "date_opened", "date_closed", "date_of_first_delinquency", "date_last_reported", "date_last_payment",
    "date_last_active", "remarks",
)

# Canonical fields the account UI shows but the reasoning engine does not see.
# Deliberately separate from RECORD_FIELDS: that tuple defines the reasoning
# prompt and the set of disputable fields, so adding display-only fields there
# would change credit reasoning. These ride along on the record for the UI.
DETAIL_FIELDS = (
    "sold_to", "account_status_raw", "account_lifecycle", "payment_performance", "report_classification", "terms", "responsibility",
    "consumer_dispute", "balance_updated_date", "date_status_updated",
    "contact", "payment_history", "source_pages", "field_evidence",
)


@dataclass
class AccountView:
    canonical: CanonicalAccount
    records: list[dict[str, Any]]  # current record per bureau
    history_count: int  # linked records including superseded snapshots
    findings: list[Finding]

    @property
    def is_negative(self) -> bool:
        return any(is_negative(r) for r in self.records)

    @property
    def strongest_severity(self) -> str | None:
        return self.findings[0].severity.value if self.findings else None


def _as_of(link: AccountLink) -> date:
    report = link.credit_account.report
    stamp = report.report_date or report.pull_date
    return stamp.date() if stamp else date.today()


def _record(link: AccountLink) -> dict[str, Any]:
    account = link.credit_account
    record = {name: getattr(account, name) for name in RECORD_FIELDS}
    record.update({name: getattr(account, name) for name in DETAIL_FIELDS})
    record.update(
        bureau=link.bureau,
        credit_account_id=str(account.id),
        report_id=str(account.report_id),
        as_of=_as_of(link).isoformat(),
        match_confidence=link.confidence,
        # Not a RECORD_FIELD, so it never reaches an AI prompt — it gates
        # whether this record may be reasoned about at all.
        extraction_status=account.report.extraction_status if account.report else None,
        original_creditor=account.original_creditor,
    )
    return record


def build_view(canonical: CanonicalAccount) -> AccountView:
    latest: dict[str, AccountLink] = {}
    for link in canonical.links:
        current = latest.get(link.bureau)
        if current is None or _as_of(link) >= _as_of(current):
            latest[link.bureau] = link

    records = [_record(link) for link in latest.values()]
    findings: list[Finding] = []
    for link, record in zip(latest.values(), records):
        findings.extend(evaluate_record(record, _as_of(link)))
    findings.extend(compare_bureau_records(records))
    return AccountView(canonical, records, len(canonical.links), sort_by_severity(findings))


def _query():
    return select(CanonicalAccount).options(
        selectinload(CanonicalAccount.links)
        .selectinload(AccountLink.credit_account)
        .selectinload(CreditAccount.report)
    )


async def load_profile(db: AsyncSession, user_id: uuid.UUID) -> list[AccountView]:
    result = await db.execute(_query().where(CanonicalAccount.user_id == user_id))
    views = [build_view(c) for c in result.scalars().all()]
    return sorted(views, key=lambda v: (v.findings[0].severity.rank if v.findings else -1), reverse=True)


async def load_account(db: AsyncSession, canonical_id: uuid.UUID) -> AccountView | None:
    result = await db.execute(_query().where(CanonicalAccount.id == canonical_id))
    canonical = result.scalar_one_or_none()
    return build_view(canonical) if canonical else None


def view_to_dict(view: AccountView) -> dict[str, Any]:
    c = view.canonical
    return {
        "id": str(c.id),
        "creditor_name": c.creditor_name,
        "account_type": c.account_type,
        "account_number_last_four": c.account_number_last_four,
        "bureaus_reporting": sorted(r["bureau"] for r in view.records),
        "is_negative": view.is_negative,
        "strongest_severity": view.strongest_severity,
        "records": view.records,
        # Set when this account closely resembled another but wasn't merged.
        "match_review": c.match_review,
        "history_count": view.history_count,
        "findings": [f.to_dict() for f in view.findings],
    }
