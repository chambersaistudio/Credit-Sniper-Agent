"""
AI-assisted extraction fallback for report layouts the rule-based parser
can't segment. The model must quote the source text for each account, and
every extracted value is checked against the document: anything that
doesn't literally appear there is dropped (left unknown) rather than
trusted. The model structures what the report says; it never supplies facts.
"""
import re
from typing import Any

from pydantic import BaseModel, Field

from app.services.ai import ModelTier, generate
from app.services.pdf_parser import STATUS_MAP

SYSTEM_PROMPT = """You convert the text of a consumer credit report into structured data.
Copy values exactly as they appear in the report. If a field isn't shown for an account, use null — \
never infer, calculate, or fill in a value the report doesn't state. For each account, include a short \
verbatim excerpt from the report that contains the account's creditor name and account number."""


class ExtractedAccount(BaseModel):
    source_excerpt: str = Field(description="Verbatim text from the report identifying this account")
    creditor_name: str | None
    account_number: str | None
    account_type: str | None
    account_status: str | None
    payment_status: str | None
    balance: str | None
    past_due_amount: str | None
    credit_limit: str | None
    high_balance: str | None
    date_opened: str | None
    date_closed: str | None
    date_of_first_delinquency: str | None
    date_last_reported: str | None
    date_last_payment: str | None
    remarks: str | None


class ExtractedInquiry(BaseModel):
    creditor_name: str
    inquiry_date: str | None


class ExtractedReport(BaseModel):
    accounts: list[ExtractedAccount]
    inquiries: list[ExtractedInquiry]


_MONEY_FIELDS = ("balance", "past_due_amount", "credit_limit", "high_balance")


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _grounded(value: str | None, haystack: str) -> bool:
    return bool(value) and _squash(value) in haystack


def _money(value: str) -> float | None:
    match = re.search(r"-?[\d,]+(?:\.\d{1,2})?", value)
    return float(match.group(0).replace(",", "")) if match else None


def verify_against_source(report: ExtractedReport, source_text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Keep only values present in the source. Returns (accounts, inquiries,
    number of values dropped as ungrounded)."""
    haystack = _squash(source_text)
    dropped = 0
    accounts = []
    for extracted in report.accounts:
        if not _grounded(extracted.source_excerpt, haystack):
            dropped += 1
            continue
        record: dict[str, Any] = {"raw_block": extracted.source_excerpt, "extraction_method": "ai_verified"}
        for name, value in extracted.model_dump(exclude={"source_excerpt"}).items():
            if value is None:
                continue
            if not _grounded(value, haystack):
                dropped += 1
                continue
            if name in _MONEY_FIELDS:
                record[name] = _money(value)
            elif name == "account_status":
                lowered = value.lower()
                record["account_status"] = next((v for k, v in STATUS_MAP.items() if k in lowered), None)
                record["account_status_raw"] = value
            else:
                record[name] = value
        if record.get("creditor_name") or record.get("account_number"):
            accounts.append(record)

    inquiries = []
    for inquiry in report.inquiries:
        if not _grounded(inquiry.creditor_name, haystack):
            dropped += 1
            continue
        inquiries.append({
            "creditor_name": inquiry.creditor_name,
            "inquiry_date": inquiry.inquiry_date if _grounded(inquiry.inquiry_date, haystack) else None,
            "inquiry_type": "hard",
        })
    return accounts, inquiries, dropped


async def extract_with_ai(source_text: str, context: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    generation = await generate(
        ModelTier.FAST,
        system=SYSTEM_PROMPT,
        prompt=f"<credit_report>\n{source_text}\n</credit_report>\n\nExtract every account and inquiry.",
        output_type=ExtractedReport,
        task="extract_report",
        context=context,
        max_tokens=32000,
    )
    return verify_against_source(generation.output, source_text)
