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
    "charge-off": "charged_off",
    "chargeoff": "charged_off",
    "collection": "collection",
    "in collections": "collection",
    "transferred": "transferred",
    "sold": "sold",
    "paid": "paid",
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
    """Single-bureau reports routinely mention the other two (dispute
    addresses, footers), so a mention alone isn't enough: one bureau must
    clearly dominate. Otherwise it's a tri-merge (all three even) or
    unknown — the caller then asks the user rather than guessing."""
    counts = {bureau: len(pattern.findall(text)) for bureau, pattern in BUREAU_PATTERNS.items()}
    mentioned = sorted((c, b) for b, c in counts.items() if c)
    if not mentioned:
        return "unknown"
    if len(mentioned) == 1:
        return mentioned[0][1]
    (second_count, _), (top_count, top) = mentioned[-2], mentioned[-1]
    if top_count >= 3 * second_count:
        return top
    return "tri_merge" if len(mentioned) == 3 else "unknown"


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

    ssn_match = re.search(r"(?:ssn|social)[:\s]+[Xx*\-\s]+(\d{4})", text, re.IGNORECASE)
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
    accounts_text = _accounts_section_only(text)

    # Prefer blank-line-separated records — the common convention, and it
    # keeps a "Creditor:" line together with its "Account Number:"/status/
    # balance lines in one block. Splitting on header keywords instead
    # (the fallback below) treats "Account Number:" as its own record
    # boundary, which fragments a single account into a creditor-only
    # piece and a number-only piece with no way to reunite them.
    account_blocks = re.split(r"\n\s*\n+", accounts_text)
    if len(account_blocks) <= 1:
        account_blocks = re.split(
            r"(?=(?:Account\s+#|Creditor|Credit\s+Card|Mortgage|Auto\s+Loan|Student\s+Loan|Collection)\s*:)",
            accounts_text,
            flags=re.IGNORECASE,
        )

    accounts = []
    for block in account_blocks:
        if len(block.strip()) < 40:
            continue

        account = _parse_account_block(block)
        if account.get("creditor_name") or account.get("account_number"):
            accounts.append(account)

    # If structured extraction finds nothing, return the full text for AI to parse
    if not accounts:
        return [{"raw_block": text, "extraction_method": "full_text_ai_parse"}]

    return accounts


_INQUIRIES_HEADER = re.compile(
    r"\b(?:hard\s+inquiries|credit\s+inquiries|inquiries\s+in\s+the\s+last[^\n]*|inquiries)\b",
    re.IGNORECASE,
)


def _accounts_section_only(text: str) -> str:
    """Cut the text off before an inquiries section, if one is present,
    so inquiry entries never get parsed as accounts."""
    match = _INQUIRIES_HEADER.search(text)
    return text[: match.start()] if match else text


# Label synonyms -> field. Every label maps to exactly one field, so e.g.
# "High Balance" can never be read as the balance and "Original Creditor"
# never as the creditor. Labels that belong to no field we store (like
# "original creditor") are still listed so they don't get swallowed by a
# shorter label inside them.
_ACCOUNT_LABELS: dict[str, tuple[str, ...]] = {
    "creditor_name": ("creditor name", "creditor", "company name", "company", "lender", "collection agency", "subscriber name"),
    "original_creditor": ("original creditor",),
    "account_number": ("account number", "account #", "account no", "acct number", "acct #", "acct no"),
    "account_type": ("account type", "type of account", "loan type"),
    "account_status": ("account status", "account condition", "condition", "status"),
    "payment_status": ("payment status", "pay status", "current payment status"),
    "balance": ("current balance", "balance owed", "balance", "amount owed"),
    "high_balance": ("high balance", "highest balance", "high credit"),
    "original_amount": ("original amount", "original loan amount"),
    "credit_limit": ("credit limit", "limit"),
    "past_due_amount": ("amount past due", "past due amount", "past due"),
    "monthly_payment": ("monthly payment", "scheduled payment"),
    "date_opened": ("date opened", "open date", "opened"),
    "date_closed": ("date closed", "closed date"),
    "date_of_first_delinquency": ("date of first delinquency", "date of 1st delinquency", "first delinquency", "dofd"),
    "date_last_reported": ("date last reported", "last reported", "date reported", "date updated", "last updated"),
    "date_last_payment": ("date of last payment", "last payment date", "last payment"),
    "date_last_active": ("date of last activity", "last activity", "last active"),
    "remarks": ("remarks", "remark", "comments", "comment"),
}
_LABEL_TO_FIELD = {label: f for f, labels in _ACCOUNT_LABELS.items() for label in labels}
_LABEL_PATTERN = re.compile(
    r"(?<![A-Za-z])("
    + "|".join(re.escape(label).replace(r"\ ", r"\s+") for label in sorted(_LABEL_TO_FIELD, key=len, reverse=True))
    + r")\s*:",
    re.IGNORECASE,
)

_MONEY_FIELDS = {"balance", "high_balance", "original_amount", "credit_limit", "past_due_amount", "monthly_payment"}
_DATE_FIELDS = {"date_opened", "date_closed", "date_of_first_delinquency", "date_last_reported", "date_last_payment", "date_last_active"}
_DATE_VALUE = re.compile(
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{1,2}(?:-\d{1,2})?|\d{1,2}[/-]\d{4}|[A-Za-z]{3,9}\.?\s+(?:\d{1,2},?\s+)?\d{4}"
)
_MONEY_VALUE = re.compile(r"\$?\s*(-?[\d,]+(?:\.\d{1,2})?)")
_ACCOUNT_NUMBER_VALUE = re.compile(r"[Xx*\d][Xx*\d\- ]{2,28}[Xx*\d]")


def _labeled_values(block: str) -> list[tuple[str, str]]:
    """(field, raw value) pairs in document order. A value runs to the next
    known label or the end of the line, whichever comes first — so both
    one-field-per-line and two-column layouts parse."""
    matches = list(_LABEL_PATTERN.finditer(block))
    pairs = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(block)
        value = block[match.end():end].split("\n", 1)[0].strip(" \t|;,")
        field = _LABEL_TO_FIELD[re.sub(r"\s+", " ", match.group(1).lower())]
        pairs.append((field, value))
    return pairs


def _parse_account_block(block: str) -> dict[str, Any]:
    account: dict[str, Any] = {"raw_block": block}

    for field, value in _labeled_values(block):
        if not value or field in account:
            continue  # first occurrence wins; never overwrite with a later, less specific one
        if field in _MONEY_FIELDS:
            money = _MONEY_VALUE.match(value)
            if money:
                account[field] = float(money.group(1).replace(",", ""))
        elif field in _DATE_FIELDS:
            date_match = _DATE_VALUE.search(value)
            if date_match:
                account[field] = date_match.group(0)
        elif field == "account_status":
            lowered = value.lower()
            for key, normalized in STATUS_MAP.items():
                if key in lowered:
                    account["account_status"] = normalized
                    break
            account["account_status_raw"] = value
        elif field == "account_number":
            number = _ACCOUNT_NUMBER_VALUE.search(value)
            if number:
                account["account_number"] = number.group(0).strip()
            elif "creditor_name" not in account:
                # Some layouts label the tradeline heading "Account #: <creditor>".
                account["creditor_name"] = value
        else:
            account[field] = value

    return account


def _extract_inquiries_raw(text: str) -> list[dict[str, Any]]:
    """Extract credit inquiry records."""
    inquiries = []

    header_match = _INQUIRIES_HEADER.search(text)
    if not header_match:
        return []

    remainder = text[header_match.end():]
    end_match = re.search(r"\b(?:accounts|public\s+records)\b", remainder, re.IGNORECASE)
    section_text = remainder[: end_match.start()] if end_match else remainder
    section_text = section_text.strip()

    # Prefer blank-line-separated records; if that collapses everything
    # into one block (each inquiry on its own single-newline-terminated
    # line, a common format), fall back to one block per line — otherwise
    # every inquiry after the first in that block would be silently
    # dropped, and a leading blank line breaks the `^` anchor below.
    inquiry_blocks = re.split(r"\n\s*\n+", section_text)
    if len(inquiry_blocks) <= 1:
        inquiry_blocks = section_text.split("\n")

    for block in inquiry_blocks:
        if len(block.strip()) < 20:
            continue

        inquiry: dict[str, Any] = {"raw_block": block}

        name_match = re.search(r"^(.+?)(?:\n|date|\d{1,2}[/-])", block, re.IGNORECASE)
        if name_match:
            inquiry["creditor_name"] = name_match.group(1).strip(" \t-—–:")

        date_match = re.search(r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\w+\s+\d{1,2},?\s+\d{4})", block)
        if date_match:
            inquiry["inquiry_date"] = date_match.group(1)

        inquiry["inquiry_type"] = "hard"
        if re.search(r"soft\s+inquiry|promotional|pre-screen|account\s+review", block, re.IGNORECASE):
            inquiry["inquiry_type"] = "soft"

        if inquiry.get("creditor_name"):
            inquiries.append(inquiry)

    return inquiries
