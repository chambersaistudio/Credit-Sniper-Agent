"""
Dispute package generation — deterministic, from the evidence graph.

Every factual sentence is rendered from stored Evidence with a fixed
template; legal references come only from the vetted catalog; the only
model-authored text is each claim's short requested remedy, which the
consumer reviews before approving. Nothing here invents a fact, a date,
or a citation — and nothing here is sent anywhere: submission is a
separate, consumer-approved step on a separate channel.
"""
from datetime import datetime, timezone
from typing import Any

from app.models.case import Case, Claim
from app.models.user import User
from app.services.bureau_directory import BUREAUS
from app.services.legal_references import REFERENCES

FIELD_LABELS = {
    "balance": "balance",
    "account_status": "account status",
    "payment_status": "payment status",
    "date_of_first_delinquency": "Date of First Delinquency",
    "date_opened": "date opened",
    "date_closed": "date closed",
    "credit_limit": "credit limit",
}
_MONEY = {"balance", "credit_limit", "past_due_amount", "high_balance"}

_LEADS = {
    "cross_bureau.balance": "The balance for this account is reported differently by different credit bureaus.",
    "cross_bureau.account_status": "The status of this account is reported inconsistently across credit bureaus.",
    "cross_bureau.dofd": (
        "The Date of First Delinquency for this account is reported inconsistently across credit bureaus. "
        "That date determines when the account must stop being reported, so only one of these dates can be accurate."
    ),
    "cross_bureau.date_opened": "The date this account was opened is reported inconsistently across credit bureaus.",
    "cross_bureau.payment_status": "The payment status of this account is reported inconsistently across credit bureaus.",
    "cross_bureau.credit_limit": "The credit limit for this account is reported inconsistently across credit bureaus.",
    "record.obsolete_reporting": "This adverse account is being reported beyond the period the law allows.",
    "record.missing_dofd": (
        "This account is reported as delinquent, charged off, or in collection without a Date of First "
        "Delinquency, so the period during which it may lawfully be reported cannot be determined."
    ),
    "record.dofd_before_opened": "The Date of First Delinquency reported for this account is earlier than the date the account was opened, which is not possible.",
    "record.closed_before_opened": "The date this account was closed is reported as earlier than the date it was opened, which is not possible.",
}

_STANDARD_REQUESTS = {
    "bureau": [
        "Conduct a reasonable reinvestigation of the information disputed above (15 U.S.C. § 1681i(a)(1)).",
        "Delete or correct any information found to be inaccurate, incomplete, or unverifiable (15 U.S.C. § 1681i(a)(5)(A)).",
        "Send me the written results of your reinvestigation and a free copy of my updated report.",
        "Provide a description of the procedure used to determine the accuracy of the disputed information, "
        "including the name and address of any furnisher you contacted (15 U.S.C. § 1681i(a)(7)).",
    ],
    "furnisher": [
        "Investigate this dispute and review all relevant information I have provided (12 C.F.R. § 1022.43).",
        "Correct or delete any information you have furnished that is inaccurate or incomplete, and notify each "
        "consumer reporting agency to which you reported it.",
        "Report this account as disputed by the consumer while your investigation is pending (15 U.S.C. § 1681s-2(a)(3)).",
        "Send me written notice of the results of your investigation.",
    ],
}

_ENCLOSURES = [
    "A copy (not the original) of a government-issued photo ID",
    "A copy of a recent utility bill or bank statement showing your current address",
    "A copy of the credit report page(s) showing this account, with the disputed items marked",
]


def _fmt(field: str, value: Any) -> str:
    if value is None:
        return "not reported"
    if field in _MONEY and isinstance(value, (int, float)):
        return f"${value:,.2f}"
    return str(value)


def render_statement(finding: dict[str, Any]) -> str:
    rule, field, values = finding.get("rule", ""), finding.get("field", ""), finding.get("values") or {}
    lead = _LEADS.get(rule, finding.get("rationale", ""))
    if rule.startswith("cross_bureau."):
        label = FIELD_LABELS.get(field, field.replace("_", " "))
        reported = "; ".join(
            f"{BUREAUS[b].display_name if b in BUREAUS else b} reports {_fmt(field, v)}" for b, v in sorted(values.items())
        )
        return f"{lead} Reported {label}: {reported}."
    if rule == "record.obsolete_reporting":
        return (
            f"{lead} The reported Date of First Delinquency is {values.get('date_of_first_delinquency')}, so the "
            f"reporting period ended on {values.get('reporting_period_end')} (15 U.S.C. § 1681c(a), (c))."
        )
    if rule in ("record.dofd_before_opened", "record.closed_before_opened"):
        details = "; ".join(f"{FIELD_LABELS.get(k, k.replace('_', ' '))}: {v}" for k, v in values.items())
        return f"{lead} Reported {details}."
    return lead


def _consumer_block(user: User) -> list[str]:
    lines = [user.full_name or "", user.address or "", f"{user.city or ''}, {user.state or ''} {user.zip_code or ''}".strip(", ")]
    if user.date_of_birth:
        lines.append(f"Date of birth: {user.date_of_birth}")
    if user.ssn_last_four:
        lines.append(f"SSN (last four): XXX-XX-{user.ssn_last_four}")
    return [line for line in lines if line.strip()]


def build_package(case: Case, claims: list[Claim], user: User, creditor_name: str, account_number: str | None) -> dict[str, Any]:
    warnings: list[str] = []
    missing = [name for name in ("full_name", "address", "city", "state", "zip_code") if not getattr(user, name)]
    if missing:
        warnings.append("Your profile is missing " + ", ".join(m.replace("_", " ") for m in missing) + ". The recipient needs these to find your file.")

    if case.recipient_type == "bureau":
        contact = BUREAUS[case.recipient_name]
        recipient_display, recipient_address = contact.display_name, contact.dispute_address
        warnings.append(f"Confirm {contact.display_name}'s current dispute mailing address before sending: {contact.dispute_url}")
    else:
        recipient_display, recipient_address = case.recipient_name, case.recipient_address
        if not recipient_address:
            warnings.append(
                "No furnisher address yet. Direct disputes must go to the address the furnisher designates for "
                "disputes (often printed on its statements or on your credit report)."
            )

    statements, remedies, reference_ids = [], [], []
    for claim in claims:
        for evidence in claim.evidence:
            # A finding about one bureau's record belongs only in that bureau's letter.
            wrong_bureau = case.recipient_type == "bureau" and evidence.bureau and evidence.bureau != case.recipient_name
            if evidence.source_type == "finding" and evidence.data and not wrong_bureau:
                statements.append({"claim_id": str(claim.id), "evidence_id": str(evidence.id), "text": render_statement(evidence.data)})
        if claim.requested_remedy:
            remedies.append(claim.requested_remedy)
        reference_ids.extend(r for r in (claim.legal_reference_ids or []) if r not in reference_ids)

    references = [
        {"id": r.id, "citation": r.citation, "title": r.title}
        for r in (REFERENCES[i] for i in reference_ids if i in REFERENCES)
        if case.recipient_type in r.applies_to
    ]
    requests = remedies + _STANDARD_REQUESTS[case.recipient_type]

    masked = f"account ending {account_number[-4:]}" if account_number and len(account_number) >= 4 else "account number as shown on my report"
    subject = f"Dispute of inaccurate information — {creditor_name}, {masked}"
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")

    body_lines = [
        *_consumer_block(user), "", today, "", recipient_display, *(recipient_address or "").splitlines(), "",
        f"Re: {subject}", "",
        "To whom it may concern:", "",
        f"I am writing to dispute information about the following account: {creditor_name}, {masked}. "
        "The specific information I dispute, and why, is set out below.", "",
    ]
    for i, statement in enumerate(statements, start=1):
        body_lines += [f"{i}. {statement['text']}", ""]
    body_lines += ["I request that you:"]
    body_lines += [f"- {r}" for r in requests]
    if references:
        body_lines += ["", "Relevant provisions: " + "; ".join(r["citation"] for r in references) + "."]
    body_lines += ["", "Copies of supporting documents are enclosed.", "", "Sincerely,", "", user.full_name or ""]

    if not statements:
        warnings.append("No evidence statements were generated for this case.")

    return {
        "recipient_type": case.recipient_type,
        "recipient": recipient_display,
        "recipient_address": recipient_address,
        "subject": subject,
        "statements": statements,
        "requests": requests,
        "legal_references": references,
        "enclosures": _ENCLOSURES,
        "body": "\n".join(body_lines).strip() + "\n",
        "warnings": warnings,
        "ready": not missing and bool(statements) and bool(recipient_address),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
