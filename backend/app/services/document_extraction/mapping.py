"""
Canonical extraction → the rows we store.

The model transcribes strings; every conversion to a number, a normalized
status or a date happens here, deterministically. That keeps "what the
document says" and "what we computed" separable, and means a model can never
produce a balance we didn't derive ourselves from printed text.
"""
import re
from typing import Any

from app.services.account_semantics import account_lifecycle, payment_performance
from app.services.document_extraction.schema import CreditReportExtraction, ExtractedTradeline
from app.services.pdf_parser import STATUS_MAP

_MONEY = re.compile(r"-?[\d,]+(?:\.\d{1,2})?")
_NORMALIZED_STATUSES = set(STATUS_MAP.values())
# Section labels that describe where the report files an account, not how it
# is being paid. They must never land in payment_status or account_status.
_REPORT_CLASSIFICATIONS = (
    "potentially negative", "exceptional payment history", "accounts in good standing",
    "negative items", "closed accounts", "open accounts",
)


def _is_report_classification(value: str | None) -> bool:
    lowered = (value or "").strip().lower()
    return bool(lowered) and any(label in lowered for label in _REPORT_CLASSIFICATIONS)


def money(value: str | None) -> float | None:
    if not value:
        return None
    match = _MONEY.search(value.replace("$", ""))
    return float(match.group(0).replace(",", "")) if match else None


def normalized_status(tradeline: ExtractedTradeline) -> str | None:
    """Trust the model's normalization only if it's one of our own values;
    otherwise derive it from the raw status the document printed. Section
    labels are excluded: "Potentially negative" describes the report's
    layout, not the account's standing."""
    proposed = (tradeline.status_normalized or "").strip().lower()
    if proposed in _NORMALIZED_STATUSES:
        return proposed
    parts = [tradeline.status_raw, tradeline.open_closed, tradeline.payment_status]
    raw = " ".join(p for p in parts if p and not _is_report_classification(p)).lower()
    return next((v for k, v in STATUS_MAP.items() if k in raw), None)


def payment_status(tradeline: ExtractedTradeline) -> str | None:
    """The account's own payment standing — never a page/section label."""
    return None if _is_report_classification(tradeline.payment_status) else tradeline.payment_status


def report_classification(tradeline: ExtractedTradeline) -> str | None:
    """The report's own section label for this account. If the extractor put
    one in payment_status or status_raw anyway, recover it here rather than
    letting it masquerade as a status."""
    if tradeline.report_classification:
        return tradeline.report_classification
    for value in (tradeline.payment_status, tradeline.status_raw):
        if _is_report_classification(value):
            return value
    return None


def payment_history(tradeline: ExtractedTradeline) -> list[dict[str, Any]] | None:
    """Keep every per-month field the report printed. The monetary and remark
    columns TransUnion prints are forensic evidence, not decoration — they are
    stored as written and never dropped for being unfamiliar."""
    if not tradeline.payment_history:
        return None
    return [
        {
            "year": e.year,
            "month": e.month,
            "status_code": e.status_code,
            "raw_status_code": e.raw_status_code,
            "balance": e.balance,
            "past_due": e.past_due,
            "amount_paid": e.amount_paid,
            "amount_due": e.amount_due,
            "remarks": e.remarks or [],
            "source_page": e.source_page,
        }
        for e in tradeline.payment_history
    ]


def field_evidence(tradeline: ExtractedTradeline) -> list[dict[str, Any]] | None:
    if not tradeline.field_evidence:
        return None
    return [
        {"field": e.field, "value": e.value, "page": e.page, "excerpt": e.excerpt}
        for e in tradeline.field_evidence
    ]


def _semantic_record(tradeline: ExtractedTradeline) -> dict[str, Any]:
    return {
        "open_closed": tradeline.open_closed,
        "date_closed": tradeline.date_closed,
        "account_status_raw": tradeline.status_raw,
        "payment_status": tradeline.payment_status,
    }


def account_row(tradeline: ExtractedTradeline) -> dict[str, Any]:
    """The CreditAccount column values for one extracted tradeline."""
    contact = tradeline.contact
    return {
        "creditor_name": tradeline.creditor_name,
        "original_creditor": tradeline.original_creditor,
        "sold_to": tradeline.sold_to,
        "account_number": tradeline.account_number,
        "account_type": tradeline.account_type,
        "account_status": normalized_status(tradeline),
        "account_status_raw": None if _is_report_classification(tradeline.status_raw) else tradeline.status_raw,
        "payment_status": payment_status(tradeline),
        # Lifecycle from the report's own open/closed field; standing from
        # the wording, normalized so bureaus that phrase it differently agree.
        "account_lifecycle": account_lifecycle(_semantic_record(tradeline)),
        "payment_performance": payment_performance(_semantic_record(tradeline)),
        "report_classification": report_classification(tradeline),
        "balance": money(tradeline.balance),
        "past_due_amount": money(tradeline.past_due_amount),
        "high_balance": money(tradeline.high_balance),
        "credit_limit": money(tradeline.credit_limit),
        "original_amount": money(tradeline.original_amount),
        "monthly_payment": money(tradeline.monthly_payment),
        "terms": tradeline.terms,
        "responsibility": tradeline.responsibility,
        "date_opened": tradeline.date_opened,
        "date_closed": tradeline.date_closed,
        "date_status_updated": tradeline.status_updated,
        "date_of_first_delinquency": tradeline.date_first_delinquency,
        # "Balance updated" is its own date. It is NOT a last-reported date,
        # so it gets its own column and date_last_reported stays null unless
        # the report actually labels one.
        "balance_updated_date": tradeline.balance_updated,
        "date_last_reported": tradeline.date_last_reported,
        "date_last_payment": tradeline.date_last_payment,
        "remarks": tradeline.remarks,
        "consumer_dispute": tradeline.consumer_dispute,
        "contact": ({"name": contact.name, "address": contact.address, "phone": contact.phone}
                    if contact and any((contact.name, contact.address, contact.phone)) else None),
        "payment_history": payment_history(tradeline),
        "source_pages": tradeline.source_pages or None,
        "field_evidence": field_evidence(tradeline),
        "raw_data": {
            "extraction_method": "ai_document",
            "identity_evidence": tradeline.identity_evidence,
            "open_closed": tradeline.open_closed,
        },
    }


INQUIRY_CATEGORIES = {
    "credit_application", "promotional", "account_review", "credit_monitoring",
    "consumer_request", "insurance", "employment", "collection", "other",
}
# Categories the bureaus themselves describe as consumer-visible only: they
# do not affect the score and must never be counted as hard inquiries.
SOFT_CATEGORIES = {"promotional", "account_review", "credit_monitoring", "consumer_request"}


def _inquiry_category(value: str | None) -> str | None:
    lowered = re.sub(r"[^a-z]+", "_", (value or "").strip().lower()).strip("_")
    if lowered in INQUIRY_CATEGORIES:
        return lowered
    if "promotion" in lowered:
        return "promotional"
    if "account_review" in lowered or "review" in lowered:
        return "account_review"
    if "monitor" in lowered:
        return "credit_monitoring"
    if "employ" in lowered:
        return "employment"
    if "insur" in lowered:
        return "insurance"
    if "collect" in lowered:
        return "collection"
    if "consumer" in lowered or "self" in lowered:
        return "consumer_request"
    if "applic" in lowered or "credit_check" in lowered:
        return "credit_application"
    return lowered or None


def _inquiry_type(value: str | None, category: str | None) -> str | None:
    """hard / soft / null — never inferred from an inquiry merely existing.

    A category the bureau calls non-scoring (promotional, account review,
    monitoring, the consumer's own request) is soft by definition. Otherwise
    we only report what the document actually said; unknown stays null rather
    than becoming a hard inquiry the consumer never incurred."""
    if category in SOFT_CATEGORIES:
        return "soft"
    lowered = (value or "").strip().lower()
    if "soft" in lowered:
        return "soft"
    if "hard" in lowered:
        return "hard"
    return None


def inquiry_rows(extraction: CreditReportExtraction) -> list[dict[str, Any]]:
    return [
        {
            "creditor_name": inquiry.creditor_name,
            "inquiry_date": inquiry.inquiry_date,
            # hard/soft/null only. An industry label like "Bank Credit Cards"
            # is a business type and is kept in its own field.
            "inquiry_type": _inquiry_type(inquiry.inquiry_type, _inquiry_category(inquiry.inquiry_category)),
            "inquiry_category": _inquiry_category(inquiry.inquiry_category),
            "business_type": inquiry.business_type,
        }
        for inquiry in extraction.inquiries
        if inquiry.creditor_name and inquiry.creditor_name.strip()
    ]


def public_records(extraction: CreditReportExtraction) -> list[dict[str, Any]] | None:
    if not extraction.public_records:
        return None
    return [r.model_dump() for r in extraction.public_records]
