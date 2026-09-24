"""
Deterministic parser for the current Experian single-bureau consumer report
(the experian.com member-report layout).

pdfplumber flattens Experian's two-column "Account info" grid into single
lines carrying up to two label/value pairs, and the labels carry NO colon:

    Account name ATLAS Balance $16
    Account number 5299XXXXXXXX1234 Balance updated Jun 25, 2026
    Original creditor - Credit limit $1,000
    Account type Line of Credit Monthly payment $16
    Date opened Dec 22, 2025 Highest balance $30
    Status Open/Never late Past due amount -

Two things the old generic (`label:`-based) parser got wrong here:
  1. It required a colon after labels, so it read none of the fields above.
  2. On collection tradelines it treated the "Original creditor" heading as
     the tradeline's identity. Experian labels the tradeline itself
     "Account name" (the collection agency) and the debt's origin
     "Original creditor" — they are different furnishers and must stay
     distinct, or a dispute is addressed to the wrong party.

Each tradeline block begins at an "Account name <name>" line; everything up
to the next such line (or the inquiries/records sections) is that account.
"""
import re
from typing import Any

from app.services.pdf_parser import STATUS_MAP

_IGNORE = "_ignore"

# Experian account-grid labels → canonical field. Labels mapped to _IGNORE are
# not stored, but are still matched so they terminate the previous value
# instead of bleeding into it (the grid is two columns per line).
_LABELS: dict[str, tuple[str, ...]] = {
    "creditor_name": ("account name",),
    "original_creditor": ("original creditor",),
    "account_number": ("account number",),
    "account_type": ("account type",),
    "account_status": ("status", "payment status"),
    "balance": ("balance", "recent balance"),
    "high_balance": ("highest balance", "high balance"),
    "credit_limit": ("credit limit",),
    "original_amount": ("original amount", "original loan amount", "loan amount"),
    "monthly_payment": ("monthly payment",),
    "past_due_amount": ("past due amount", "amount past due", "past due"),
    "date_opened": ("date opened",),
    "date_closed": ("date closed", "closed date"),
    "date_last_reported": ("balance updated", "status updated", "date updated", "last reported"),
    "date_last_payment": ("date of last payment", "last payment"),
    "date_last_active": ("date of last activity", "last active"),
    "remarks": ("comment", "comments", "remarks"),
    _IGNORE: ("responsibility", "terms", "credit usage", "payment history",
              "on record until", "your statement", "recent payment", "dispute status"),
}
_LABEL_TO_FIELD = {syn: field for field, syns in _LABELS.items() for syn in syns}
_ALL_LABELS = sorted(_LABEL_TO_FIELD, key=len, reverse=True)
# Colonless, whole-label match (an optional trailing colon is handled by the
# non-letter lookahead, so "Account name" and "Account name:" both match).
_LABEL_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(re.escape(s).replace(r"\ ", r"\s+") for s in _ALL_LABELS) + r")(?![A-Za-z])",
    re.IGNORECASE,
)

_ACCOUNT_ANCHOR = re.compile(r"(?im)^[ \t]*account name(?![A-Za-z])")
# Sections that end the accounts area, so the last tradeline block doesn't
# swallow inquiries or public records.
_SECTION_END = re.compile(
    r"(?im)^[ \t]*(?:hard inquiries|soft inquiries|inquiries|public records|"
    r"personal information|creditor contacts|account summary|contact information)\b"
)

_MONEY_FIELDS = {"balance", "high_balance", "original_amount", "credit_limit", "past_due_amount", "monthly_payment"}
_DATE_FIELDS = {"date_opened", "date_closed", "date_last_reported", "date_last_payment", "date_last_active",
                "date_of_first_delinquency"}
_TEXT_FIELDS = {"creditor_name", "original_creditor", "account_number", "account_type", "remarks"}

_MONEY_VALUE = re.compile(r"-?\$?\s*[\d,]+(?:\.\d{1,2})?")
_DATE_VALUE = re.compile(
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{1,2}(?:-\d{1,2})?|\d{1,2}[/-]\d{4}"
    r"|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|[A-Za-z]{3,9}\.?\s+\d{4}"
)
_MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
_INQUIRY_PAIR = re.compile(
    r"([A-Za-z][A-Za-z0-9 &.,'/\-]{1,38}?)\s+((?:" + _MONTHS + r")[a-z]*\.?\s+\d{1,2},?\s+\d{4})"
)
# Obvious non-creditor debris the old inquiry parser let through.
_NOT_A_CREDITOR = re.compile(r"prepared for|^\(?\d|address|phone|report", re.IGNORECASE)


def _norm(label: str) -> str:
    return re.sub(r"\s+", " ", label).strip().lower()


def _clean(value: str) -> str:
    return value.strip(" \t:|·").strip(" \t-–—").strip()


def _pairs(line: str) -> list[tuple[str, str]]:
    """(field, raw value) for each labelled cell on a flattened grid line.
    A value runs from its label to the next label (or end of line)."""
    matches = list(_LABEL_RE.finditer(line))
    out = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
        out.append((_LABEL_TO_FIELD[_norm(m.group(1))], _clean(line[m.end():end])))
    return out


def _typed(field: str, value: str) -> Any:
    if not value or value in ("-", "--", "—", "N/A", "n/a"):
        return None
    if field in _MONEY_FIELDS:
        m = _MONEY_VALUE.search(value)
        return float(m.group(0).replace("$", "").replace(",", "").replace(" ", "")) if m else None
    if field in _DATE_FIELDS:
        m = _DATE_VALUE.search(value)
        return m.group(0) if m else None
    return value


def _parse_block(block: str) -> dict[str, Any]:
    account: dict[str, Any] = {"raw_block": block, "extraction_method": "experian_parser"}
    for field, raw in _pairs(block):
        if field == _IGNORE or field in account and field not in ("raw_block",):
            continue  # first labelled occurrence of a field wins
        if field == "account_status":
            if raw:
                lowered = raw.lower()
                account["account_status"] = next((v for k, v in STATUS_MAP.items() if k in lowered), None)
                account["account_status_raw"] = raw
            continue
        value = _typed(field, raw)
        if value is not None:
            account[field] = value
    return account


def looks_like_experian(text: str) -> bool:
    """True when the report uses the labelled Experian account grid."""
    return bool(_ACCOUNT_ANCHOR.search(text))


def parse_experian_accounts(text: str) -> list[dict[str, Any]]:
    anchors = [m.start() for m in _ACCOUNT_ANCHOR.finditer(text)]
    if not anchors:
        return []
    accounts = []
    for i, start in enumerate(anchors):
        end = anchors[i + 1] if i + 1 < len(anchors) else len(text)
        block = text[start:end]
        cut = _SECTION_END.search(block)
        if cut:
            block = block[: cut.start()]
        account = _parse_block(block)
        if account.get("creditor_name"):
            accounts.append(account)
    return accounts


def _inquiries_section(text: str) -> str:
    start = re.search(r"(?im)^[ \t]*hard inquiries\b", text)
    if not start:
        return ""
    rest = text[start.end():]
    end = re.search(r"(?im)^[ \t]*(?:soft inquiries|personal information|creditor contacts|"
                    r"public records|account information|accounts)\b", rest)
    return rest[: end.start()] if end else rest


def parse_experian_inquiries(text: str) -> list[dict[str, Any]]:
    """Experian lists hard inquiries as (creditor, date) — often two per line
    in a two-column grid. Pull name/date pairs and drop debris (phone numbers,
    "Prepared For …", addresses) the old parser mistook for inquiries."""
    section = _inquiries_section(text)
    if not section:
        return []
    inquiries = []
    seen = set()
    for name, date in _INQUIRY_PAIR.findall(section):
        name = _clean(name)
        if not name or _NOT_A_CREDITOR.search(name) or not re.search(r"[A-Za-z]{2}", name):
            continue
        key = (name.lower(), date)
        if key in seen:
            continue
        seen.add(key)
        inquiries.append({"creditor_name": name, "inquiry_date": date.strip(), "inquiry_type": "hard"})
    return inquiries
