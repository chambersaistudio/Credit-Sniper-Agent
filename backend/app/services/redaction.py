"""
Deterministic PII redaction for text that leaves the server for an AI
provider. Pure regex/string logic — no model is involved, so redaction can't
itself leak or hallucinate.

Three layers, applied in order:
  1. Known identity values: the consumer's own name, address, DOB, phone,
     and email (from their profile and the report header), in the spellings
     reports actually use ("SMITH, JOHN M", "January 15, 1985", ...).
  2. Labeled identity lines ("SSN:", "Date of Birth:", "Also Known As:",
     "Employer:", ...). Labels must start the line, so "Creditor Name:" and
     "Account Name:" are never matched by the plain "Name:" label.
  3. Patterns: SSNs (full or masked), email addresses, phone numbers, street
     addresses, PO boxes, and city/state/ZIP.

Tradeline data is left intact: creditor names, masked account numbers,
balances, statuses, account dates, payment history, and remarks. Street
addresses and phone numbers are removed wherever they appear — including a
creditor's own contact details — because extraction never needs them.
"""
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

from app.utils.dates import parse_report_date


def token(kind: str) -> str:
    return f"[REDACTED {kind}]"


@dataclass
class Identity:
    """What we already know about the consumer, used for exact-value redaction."""

    names: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)
    dates_of_birth: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)

    @classmethod
    def from_sources(cls, personal_info: dict | None = None, user=None) -> "Identity":
        info = personal_info or {}
        identity = cls(
            names=[info.get("name", "")],
            addresses=[info.get("address", "")],
            dates_of_birth=[info.get("date_of_birth", "")],
        )
        if user is not None:
            identity.names.append(user.full_name or "")
            identity.addresses.append(user.address or "")
            identity.dates_of_birth.append(user.date_of_birth or "")
            identity.phones.append(user.phone or "")
            identity.emails.append(user.email or "")
        for attr in ("names", "addresses", "dates_of_birth", "phones", "emails"):
            setattr(identity, attr, [v.strip() for v in getattr(identity, attr) if v and v.strip()])
        return identity


@dataclass
class RedactionResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


US_STATES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
    "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR GU VI AS MP"
).split()

_STREET_SUFFIX = (
    r"street|st|avenue|ave|road|rd|boulevard|blvd|drive|dr|lane|ln|court|ct|way|place|pl|circle|cir|"
    r"terrace|ter|parkway|pkwy|highway|hwy|trail|trl|square|sq|loop|run|path|pike|row|alley|aly"
)

# Labels whose value on the same line is identity information. Anchored to
# the start of a line; longest alternatives first.
_IDENTITY_LABELS = sorted([
    "name", "full name", "consumer name", "consumer", "report for", "prepared for", "personal information",
    "also known as", "aka", "a.k.a.", "name variation", "name variations", "other names", "names reported",
    "address", "addresses", "current address", "previous address", "previous addresses", "former address",
    "former addresses", "prior address", "mailing address", "residence", "addresses reported", "street",
    "city", "zip", "zip code", "postal code",
    "date of birth", "birth date", "birthdate", "dob", "year of birth", "born",
    "social security number", "social security", "social security #", "ssn", "ss#",
    "phone", "phone number", "phone numbers", "telephone", "home phone", "work phone", "mobile", "cell",
    "email", "e-mail", "email address",
    "employer", "employers", "employment", "employment history", "occupation", "position",
    "spouse", "spouse name", "driver's license", "drivers license", "license number",
], key=len, reverse=True)
_LABELED_LINE = re.compile(
    r"(?im)^(?P<prefix>[ \t]*(?:" + "|".join(re.escape(l).replace(r"\ ", r"[ \t]+") for l in _IDENTITY_LABELS)
    + r")[ \t]*[:#][ \t]*)(?P<value>[^\n]*)$"
)
# Labels whose value may sit on the following line(s) instead.
_BLOCK_LABELS = re.compile(
    r"(?im)^[ \t]*(?:current address|previous address(?:es)?|former address(?:es)?|addresses|address|"
    r"also known as|name variations?|employment history|employers?)[ \t]*:?[ \t]*\n"
    # Up to three following lines — but only colon-free ones, so an adjacent
    # labeled field such as "Creditor: ..." is never swallowed.
    r"(?P<lines>(?:[ \t]*[^\s:][^:\n]*(?:\n|$)){1,3})"
)

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("SSN", re.compile(r"(?<![\w-])\d{3}[- ]\d{2}[- ]\d{4}(?![\w-])")),
    # Exactly 3-2-4 masked: can't start mid-way through a longer masked run
    # like the account number "6011********7788".
    ("SSN", re.compile(r"(?<![\w*#-])[Xx*#]{3}[- ]?[Xx*#]{2}[- ]?\d{4}(?![\w-])")),
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("PHONE", re.compile(r"(?<![\w-])(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?![\w-])")),
    ("ADDRESS", re.compile(
        r"(?i)(?<![\w#])\d{1,6}(?:\s+[A-Za-z0-9.'-]+){1,5}?\s+(?:" + _STREET_SUFFIX + r")\b\.?"
        r"(?:\s*,?\s*(?:apt|apartment|unit|suite|ste|#)\.?\s*#?[A-Za-z0-9-]+)?"
    )),
    ("ADDRESS", re.compile(r"(?i)\bP\.?\s?O\.?\s*Box\s+\d+")),
    ("LOCATION", re.compile(
        r"(?:[A-Za-z][A-Za-z.'-]*[ \t]){0,2}[A-Za-z][A-Za-z.'-]*,?[ \t]+(?:" + "|".join(US_STATES) + r")[ \t]+\d{5}(?:-\d{4})?(?!\d)"
    )),
]


def _flexible(value: str) -> str:
    """Regex for a literal value that tolerates whitespace/punctuation differences."""
    parts = [re.escape(p) for p in re.split(r"[\s,.]+", value.strip()) if p]
    return r"[\s,.]+".join(parts)


def _name_variants(name: str) -> set[str]:
    words = [w for w in re.split(r"[\s,]+", name.strip()) if w]
    variants = {name}
    if len(words) >= 2:
        first, last = words[0], words[-1]
        middle = words[1:-1]
        variants |= {f"{first} {last}", f"{last}, {first}", f"{last} {first}"}
        if middle:
            initial = middle[0][0]
            variants |= {f"{first} {initial} {last}", f"{first} {initial}. {last}",
                         f"{last}, {first} {' '.join(middle)}", f"{last}, {first} {initial}", f"{last} {first} {initial}"}
    return {v for v in variants if len(v.replace(" ", "")) >= 4}


def _dob_variants(value: str) -> set[str]:
    parsed = parse_report_date(value)
    variants = {value}
    if isinstance(parsed, date):
        m, d, y = parsed.month, parsed.day, parsed.year
        variants |= {
            f"{m:02d}/{d:02d}/{y}", f"{m}/{d}/{y}", f"{m:02d}-{d:02d}-{y}", f"{y}-{m:02d}-{d:02d}",
            f"{m:02d}/{d:02d}/{y % 100:02d}", parsed.strftime("%B %d, %Y"), parsed.strftime("%b %d, %Y"),
            f"{parsed.strftime('%B')} {d}, {y}",
        }
    return variants


def _phone_pattern(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)[-10:]
    if len(digits) != 10:
        return None
    return r"(?<!\d)(?:\+?1\D{0,2})?" + r"\D{0,2}".join(digits) + r"(?!\d)"


def _sub(pattern: str | re.Pattern, kind: str, text: str, counts: dict[str, int], flags=re.IGNORECASE) -> str:
    compiled = pattern if isinstance(pattern, re.Pattern) else re.compile(pattern, flags)
    new, n = compiled.subn(token(kind), text)
    if n:
        counts[kind] = counts.get(kind, 0) + n
    return new


def redact(text: str, identity: Identity | None = None) -> RedactionResult:
    counts: dict[str, int] = {}
    identity = identity or Identity()

    # 1. Known identity values, longest first so a full name goes before its parts.
    known: list[tuple[str, str]] = []
    for name in identity.names:
        known += [(_flexible(v), "NAME") for v in _name_variants(name)]
    for address in identity.addresses:
        known.append((_flexible(address), "ADDRESS"))
    for dob in identity.dates_of_birth:
        known += [(re.escape(v), "DOB") for v in _dob_variants(dob)]
    for email in identity.emails:
        known.append((re.escape(email), "EMAIL"))
    for pattern, kind in sorted(known, key=lambda k: len(k[0]), reverse=True):
        text = _sub(r"(?<![\w])" + pattern + r"(?![\w])", kind, text, counts)
    for phone in identity.phones:
        if (pattern := _phone_pattern(phone)) is not None:
            text = _sub(pattern, "PHONE", text, counts)

    # 2. Labeled identity lines (value on the same line, or on the next lines).
    def _block(match: re.Match) -> str:
        counts["IDENTITY"] = counts.get("IDENTITY", 0) + 1
        return match.group(0)[: match.start("lines") - match.start(0)] + token("IDENTITY") + "\n"

    text = _BLOCK_LABELS.sub(_block, text)

    def _line(match: re.Match) -> str:
        value = match.group("value")
        if not value.strip() or value.strip().startswith("[REDACTED"):
            return match.group(0)
        counts["IDENTITY"] = counts.get("IDENTITY", 0) + 1
        return match.group("prefix") + token("IDENTITY")

    text = _LABELED_LINE.sub(_line, text)

    # 3. Patterns.
    for kind, pattern in _PATTERNS:
        text = _sub(pattern, kind, text, counts)

    return RedactionResult(text=text, counts=counts)


def redact_values(values: Iterable[str], identity: Identity | None = None) -> list[str]:
    return [redact(v, identity).text for v in values]
