"""
Canonical extraction → the rows we store.

The model transcribes strings; every conversion to a number, a normalized
status or a date happens here, deterministically. That keeps "what the
document says" and "what we computed" separable, and means a model can never
produce a balance we didn't derive ourselves from printed text.
"""
import re
from typing import Any

from app.services.document_extraction.schema import CreditReportExtraction, ExtractedTradeline
from app.services.pdf_parser import STATUS_MAP

_MONEY = re.compile(r"-?[\d,]+(?:\.\d{1,2})?")
_NORMALIZED_STATUSES = set(STATUS_MAP.values())


def money(value: str | None) -> float | None:
    if not value:
        return None
    match = _MONEY.search(value.replace("$", ""))
    return float(match.group(0).replace(",", "")) if match else None


def normalized_status(tradeline: ExtractedTradeline) -> str | None:
    """Trust the model's normalization only if it's one of our own values;
    otherwise derive it from the raw status the document printed."""
    proposed = (tradeline.status_normalized or "").strip().lower()
    if proposed in _NORMALIZED_STATUSES:
        return proposed
    raw = " ".join(filter(None, [tradeline.status_raw, tradeline.open_closed, tradeline.payment_status])).lower()
    return next((v for k, v in STATUS_MAP.items() if k in raw), None)


def payment_history(tradeline: ExtractedTradeline) -> list[dict[str, Any]] | None:
    if not tradeline.payment_history:
        return None
    return [
        {"year": e.year, "month": e.month, "raw_code": e.raw_code, "code": e.code}
        for e in tradeline.payment_history
    ]


def field_evidence(tradeline: ExtractedTradeline) -> list[dict[str, Any]] | None:
    if not tradeline.field_evidence:
        return None
    return [
        {"field": e.field, "value": e.value, "page": e.page, "excerpt": e.excerpt}
        for e in tradeline.field_evidence
    ]


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
        "account_status_raw": tradeline.status_raw,
        "payment_status": tradeline.payment_status,
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
        "date_last_reported": tradeline.date_last_reported or tradeline.balance_updated,
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
            "balance_updated": tradeline.balance_updated,
            "open_closed": tradeline.open_closed,
        },
    }


def inquiry_rows(extraction: CreditReportExtraction) -> list[dict[str, Any]]:
    return [
        {
            "creditor_name": inquiry.creditor_name,
            "inquiry_date": inquiry.inquiry_date,
            "inquiry_type": (inquiry.inquiry_type or "hard").strip().lower(),
        }
        for inquiry in extraction.inquiries
        if inquiry.creditor_name and inquiry.creditor_name.strip()
    ]


def public_records(extraction: CreditReportExtraction) -> list[dict[str, Any]] | None:
    if not extraction.public_records:
        return None
    return [r.model_dump() for r in extraction.public_records]
