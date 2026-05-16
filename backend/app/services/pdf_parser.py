"""
Credit report PDF parser. Handles tri-merge and single-bureau reports.
Extracts accounts, inquiries, and personal info sections.
"""
import re
import logging
from pathlib import Path
from typing import Any
from datetime import datetime

import pdfplumber

logger = logging.getLogger(__name__)

# Bureau detection patterns
BUREAU_PATTERNS = {
    "equifax": re.compile(r"equifax", re.IGNORECASE),
    "experian": re.compile(r"experian", re.IGNORECASE),
    "transunion": re.compile(r"trans\s*union", re.IGNORECASE),
}

# Account status mapping
STATUS_MAP = {
    "charge off": "charged_off",
    "charged off": "charged_off",
    "collection": "collection",
    "in collections": "collection",
    "closed": "closed",
    "open": "open",
    "current": "current",
    "pays as agreed": "current",
    "derogatory": "derogatory",
    "late": "late",
    "30": "30d_late",
    "60": "60d_late",
    "90": "90d_late",
    "120": "120d_late",
}

# 7-year FCRA reporting limit (most negative items)
FCRA_7_YEAR_LIMIT = 7
# 10-year limit for Chapter 7 bankruptcy
FCRA_10_YEAR_LIMIT = 10


def parse_credit_report_pdf(file_path: str) -> dict[str, Any]:
    """
    Parse a credit report PDF and return structured data.
    Returns raw text + structured sections for AI analysis.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {file_path}")

    full_text = ""
    pages_text = []

    try:
        with pdfplumber.open(file_path) as pdf:
            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                pages_text.append({"page": i + 1, "text": text})
                full_text += text + "\n"
    except Exception as e:
        logger.error(f"pdfplumber failed: {e}, trying pypdf fallback")
        full_text = _pypdf_fallback(file_path)

    bureau = _detect_bureau(full_text)
    credit_score = _extract_credit_score(full_text)
    personal_info = _extract_personal_info(full_text)
    accounts = _extract_accounts_raw(full_text)
    inquiries = _extract_inquiries_raw(full_text)
    report_date = _extract_report_date(full_text)

    return {
        "raw_text": full_text,
        "bureau": bureau,
        "credit_score": credit_score,
        "report_date": report_date,
        "personal_info": personal_info,
        "accounts_raw": accounts,
        "inquiries_raw": inquiries,
        "pages": len(pages_text),
        "parse_timestamp": datetime.utcnow().isoformat(),
    }


def _pypdf_fallback(file_path: str) -> str:
    from pypdf import PdfReader
    reader = PdfReader(file_path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _detect_bureau(text: str) -> str:
    detected = []
    for bureau, pattern in BUREAU_PATTERNS.items():
        if pattern.search(text):
            detected.append(bureau)
    if len(detected) >= 2:
        return "tri_merge"
    return detected[0] if detected else "unknown"


def _extract_credit_score(text: str) -> int | None:
    # FICO score patterns: 3-digit numbers in 300-850 range near score keywords
    patterns = [
        r"(?:credit\s+score|fico\s+score|vantage\s+score)[:\s]+(\d{3})",
        r"(\d{3})\s+(?:credit\s+score|fico)",
        r"score[:\s]+(\d{3})\b",
        r"\b([3-8]\d{2})\b.*(?:score|rating)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            score = int(match.group(1))
            if 300 <= score <= 850:
                return score
    return None


def _extract_credit_score_multi(text: str) -> dict[str, int]:
    """Extract scores per bureau for tri-merge reports."""
    scores = {}
    bureau_score_pattern = re.compile(
        r"(equifax|experian|trans\s*union)[^\n]*?([3-8]\d{2})", re.IGNORECASE
    )
    for match in bureau_score_pattern.finditer(text):
        bureau = match.group(1).lower().replace(" ", "")
        if bureau == "transunion":
            bureau = "transunion"
        score = int(match.group(2))
        if 300 <= score <= 850:
            scores[bureau] = score
    return scores


def _extract_personal_info(text: str) -> dict[str, str]:
    info = {}

    name_match = re.search(r"(?:name|consumer)[:\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)", text)
    if name_match:
        info["name"] = name_match.group(1)

    ssn_match = re.search(r"(?:ssn|social)[:\s]+[Xx*]+(\d{4})", text)
    if ssn_match:
        info["ssn_last_four"] = ssn_match.group(1)

    dob_match = re.search(r"(?:date of birth|dob|born)[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text, re.IGNORECASE)
    if dob_match:
        info["date_of_birth"] = dob_match.group(1)

    addr_match = re.search(r"(?:address|current address)[:\s]+(.+?)(?:\n|city|state)", text, re.IGNORECASE)
    if addr_match:
        info["address"] = addr_match.group(1).strip()

    return info


def _extract_report_date(text: str) -> str | None:
    patterns = [
        r"(?:report\s+date|date\s+of\s+report|generated|as\s+of)[:\s]+(\w+\s+\d{1,2},?\s+\d{4})",
        r"(?:report\s+date|date\s+of\s+report)[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _extract_accounts_raw(text: str) -> list[dict[str, Any]]:
    """
    Extract raw account blocks from credit report text.
    This is a best-effort structured extraction; the AI analysis engine
    will do deeper interpretation.
    """
    accounts = []

    # Split on common account header patterns
    account_blocks = re.split(
        r"(?=(?:Account\s+#|Account\s+Number|Creditor|Credit\s+Card|Mortgage|Auto\s+Loan|Student\s+Loan|Collection)\s*:)",
        text,
        flags=re.IGNORECASE,
    )

    for block in account_blocks:
        if len(block.strip()) < 50:
            continue

        account = _parse_account_block(block)
        if account.get("creditor_name") or account.get("account_number"):
            accounts.append(account)

    # If structured extraction finds nothing, return the full text for AI to parse
    if not accounts:
        return [{"raw_block": text, "extraction_method": "full_text_ai_parse"}]

    return accounts


def _parse_account_block(block: str) -> dict[str, Any]:
    account = {"raw_block": block}

    creditor_match = re.search(
        r"(?:creditor|company|lender|collection\s+agency)[:\s]+(.+?)(?:\n|account)", block, re.IGNORECASE
    )
    if creditor_match:
        account["creditor_name"] = creditor_match.group(1).strip()

    acct_match = re.search(
        r"(?:account\s*(?:#|number|num)|acct)[:\s#]+([X*\d-]{4,20})", block, re.IGNORECASE
    )
    if acct_match:
        account["account_number"] = acct_match.group(1).strip()

    balance_match = re.search(
        r"(?:balance|amount\s+owed)[:\s]+\$?([\d,]+(?:\.\d{2})?)", block, re.IGNORECASE
    )
    if balance_match:
        account["balance"] = float(balance_match.group(1).replace(",", ""))

    limit_match = re.search(
        r"(?:credit\s+limit|high\s+balance|original\s+amount)[:\s]+\$?([\d,]+(?:\.\d{2})?)", block, re.IGNORECASE
    )
    if limit_match:
        account["credit_limit"] = float(limit_match.group(1).replace(",", ""))

    status_text = ""
    status_match = re.search(
        r"(?:account\s+status|status|payment\s+status)[:\s]+(.+?)(?:\n|date|balance)", block, re.IGNORECASE
    )
    if status_match:
        status_text = status_match.group(1).strip().lower()
        for key, val in STATUS_MAP.items():
            if key in status_text:
                account["account_status"] = val
                break

    for date_field, pattern in [
        ("date_opened", r"(?:date\s+opened|opened)[:\s]+(\w+\s+\d{4}|\d{1,2}[/-]\d{4})"),
        ("date_closed", r"(?:date\s+closed|closed)[:\s]+(\w+\s+\d{4}|\d{1,2}[/-]\d{4})"),
        ("date_of_first_delinquency", r"(?:date\s+of\s+first\s+delinquency|first\s+delinquency|dofd)[:\s]+(\w+\s+\d{4}|\d{1,2}[/-]\d{4})"),
        ("date_last_reported", r"(?:date\s+(?:last\s+)?reported|last\s+reported)[:\s]+(\w+\s+\d{4}|\d{1,2}[/-]\d{4})"),
    ]:
        match = re.search(pattern, block, re.IGNORECASE)
        if match:
            account[date_field] = match.group(1)

    type_match = re.search(
        r"(?:account\s+type|type)[:\s]+(.+?)(?:\n|status|balance)", block, re.IGNORECASE
    )
    if type_match:
        account["account_type"] = type_match.group(1).strip()

    return account


def _extract_inquiries_raw(text: str) -> list[dict[str, Any]]:
    """Extract credit inquiry records."""
    inquiries = []

    inquiry_section = re.search(
        r"(?:hard\s+inquiries|credit\s+inquiries|inquiries\s+in\s+the\s+last)(.+?)(?:accounts|public\s+records|\Z)",
        text,
        re.IGNORECASE | re.DOTALL,
    )

    if not inquiry_section:
        return []

    section_text = inquiry_section.group(1)
    inquiry_blocks = re.split(r"\n{2,}", section_text)

    for block in inquiry_blocks:
        if len(block.strip()) < 20:
            continue

        inquiry: dict[str, Any] = {"raw_block": block}

        name_match = re.search(r"^(.+?)(?:\n|date|\d{1,2}[/-])", block, re.IGNORECASE)
        if name_match:
            inquiry["creditor_name"] = name_match.group(1).strip()

        date_match = re.search(r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\w+\s+\d{1,2},?\s+\d{4})", block)
        if date_match:
            inquiry["inquiry_date"] = date_match.group(1)

        inquiry["inquiry_type"] = "hard"
        if re.search(r"soft\s+inquiry|promotional|pre-screen|account\s+review", block, re.IGNORECASE):
            inquiry["inquiry_type"] = "soft"

        if inquiry.get("creditor_name"):
            inquiries.append(inquiry)

    return inquiries


def check_7_year_rule(date_of_first_delinquency: str, report_date: str | None = None) -> bool:
    """
    Returns True if account has exceeded 7-year FCRA reporting limit.
    Uses DOFD (date of first delinquency) as the start of the 7-year clock.
    """
    try:
        from dateutil import parser as dateparser
        dofd = dateparser.parse(date_of_first_delinquency)
        if report_date:
            ref_date = dateparser.parse(report_date)
        else:
            ref_date = datetime.now()

        years_elapsed = (ref_date - dofd).days / 365.25
        return years_elapsed > FCRA_7_YEAR_LIMIT
    except Exception:
        return False
