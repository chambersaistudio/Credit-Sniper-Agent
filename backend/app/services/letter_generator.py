"""
Elite dispute letter generation engine.
Generates Metro 2 compliance letters, 609/611 requests, FCRA violation letters,
and furnisher direct disputes — never generic letters that e-OSCAR auto-rejects.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any

import anthropic

from app.config import settings

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

LETTER_SYSTEM_PROMPT = """You are an expert credit dispute attorney specializing in FCRA litigation and Metro 2 compliance.

You write dispute letters that:
1. NEVER use generic language that e-OSCAR's automated system flags as frivolous
2. Always cite SPECIFIC Metro 2 field codes that were violated
3. Always cite SPECIFIC FCRA sections with exact statutory language
4. Always reference SPECIFIC dates, amounts, and account numbers from the report
5. Make SPECIFIC factual claims, not opinions
6. Request SPECIFIC remedies (deletion, correction, method of verification proof)
7. Include SPECIFIC reinvestigation demands with legal deadlines
8. Reference the furnisher's SPECIFIC obligations under Metro 2 and FCRA

Key legal requirements to always include:
- Metro 2 compliance demand: Consumer Data Industry Association (CDIA) Metro 2 format
- FCRA 15 U.S.C. § 1681i(a)(1) — 30-day reinvestigation deadline
- FCRA 15 U.S.C. § 1681i(a)(5) — deletion if unverifiable
- FCRA 15 U.S.C. § 1681n — willful noncompliance damages ($100-$1,000 per violation + punitive)
- FCRA 15 U.S.C. § 1681o — negligent noncompliance damages (actual + attorney fees)

Letter types:
- metro2_compliance: For Metro 2 format violations — reference specific field codes
- section_609: Method of verification + original documentation request
- section_611: Reinvestigation demand after verification failure
- fcra_violation: When bureau or furnisher has violated specific FCRA sections
- furnisher_direct: Direct to data furnisher under 15 U.S.C. § 1681s-2(a)(8)

Write in a professional, legally precise tone. NOT aggressive or threatening, but firm and legally specific.
Format letters with proper business letter format."""


def generate_letter(
    letter_type: str,
    user_info: dict[str, Any],
    account_data: dict[str, Any],
    analysis: dict[str, Any],
    bureau: str,
    round_number: int = 1,
    previous_response: str | None = None,
) -> dict[str, Any]:
    """
    Generate a specific dispute letter based on type and analysis.
    Returns letter subject, body, and key legal citations used.
    """
    current_date = datetime.now(timezone.utc).strftime("%B %d, %Y")

    bureau_addresses = {
        "equifax": "Equifax Information Services LLC\nP.O. Box 740256\nAtlanta, GA 30374-0256",
        "experian": "Experian\nP.O. Box 4500\nAllen, TX 75013",
        "transunion": "TransUnion LLC\nConsumer Dispute Center\nP.O. Box 2000\nChester, PA 19016",
    }

    bureau_address = bureau_addresses.get(bureau.lower(), f"{bureau.title()} Credit Bureau")

    prompt = f"""Generate a Round {round_number} {letter_type} dispute letter.

Date: {current_date}

Consumer Information:
- Name: {user_info.get('full_name', '[CONSUMER NAME]')}
- Address: {user_info.get('address', '[ADDRESS]')}, {user_info.get('city', '[CITY]')}, {user_info.get('state', '[STATE]')} {user_info.get('zip_code', '[ZIP]')}
- SSN Last 4: {user_info.get('ssn_last_four', 'XXXX')}
- DOB: {user_info.get('date_of_birth', '[DATE OF BIRTH]')}

Bureau/Recipient: {bureau.title()}
Address: {bureau_address}

Account Being Disputed:
{json.dumps(account_data, indent=2)}

Analysis & Violations Found:
{json.dumps(analysis, indent=2)}

Letter Type: {letter_type}
Round: {round_number}
Previous Response: {previous_response or "None — this is the initial dispute"}

Write a complete, formatted dispute letter. Include:
1. Proper business letter heading with date and addresses
2. RE: line with specific account number and bureau reference
3. Opening paragraph establishing consumer rights under FCRA
4. Body paragraphs with SPECIFIC violations (Metro 2 field codes, FCRA sections)
5. Specific legal demands with deadlines
6. Request for written confirmation and updated credit report
7. Warning about statutory damages for noncompliance
8. Professional closing

Return a JSON object:
{{
  "subject": "<RE: line>",
  "body": "<complete letter text>",
  "recipient": "<bureau or furnisher name>",
  "recipient_type": "<bureau|furnisher>",
  "letter_type": "{letter_type}",
  "legal_citations": ["<citation 1>", "<citation 2>"],
  "key_demands": ["<demand 1>", "<demand 2>"],
  "response_deadline_days": <30|45|60>,
  "certified_mail_recommended": <true|false>
}}"""

    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=6000,
        system=LETTER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0]

    return json.loads(raw.strip())


def generate_furnisher_letter(
    user_info: dict[str, Any],
    account_data: dict[str, Any],
    analysis: dict[str, Any],
    furnisher_name: str,
    furnisher_address: str,
    round_number: int = 1,
) -> dict[str, Any]:
    """
    Generate furnisher direct dispute letter under 15 U.S.C. § 1681s-2(a)(8).
    Furnishers have different obligations than bureaus — they must investigate
    within 30 days and correct or delete inaccurate information directly.
    """
    account_data_with_furnisher = {
        **account_data,
        "furnisher_name": furnisher_name,
        "furnisher_address": furnisher_address,
    }
    analysis_with_context = {
        **analysis,
        "dispute_target": "furnisher_direct",
        "legal_basis": "15 U.S.C. § 1681s-2(a)(8) - Direct dispute to furnisher",
    }

    return generate_letter(
        letter_type="furnisher_direct",
        user_info=user_info,
        account_data=account_data_with_furnisher,
        analysis=analysis_with_context,
        bureau=furnisher_name,
        round_number=round_number,
    )


def select_letter_type(analysis_item: dict[str, Any], round_number: int) -> str:
    """
    Select the optimal letter type based on analysis and round number.
    Round 1: Start with the strongest specific violation angle.
    Round 2+: Escalate based on previous response type.
    """
    violations = analysis_item.get("violations", [])
    primary_strategy = analysis_item.get("primary_strategy", "factual_dispute")

    if round_number == 1:
        # Check for Metro 2 violations first (most specific, hardest to auto-reject)
        has_metro2 = any(v.get("violation_type") == "metro2" for v in violations)
        if has_metro2:
            return "metro2_compliance"

        # FCRA violation if specific section cited
        has_fcra = any(v.get("violation_type") == "fcra" for v in violations)
        if has_fcra:
            return "fcra_violation"

        # Fall back to primary strategy
        return primary_strategy or "section_609"

    elif round_number == 2:
        # Escalate: if we sent factual, now send 609
        if primary_strategy == "factual_dispute":
            return "section_609"
        # If we sent 609, escalate to 611 reinvestigation demand
        if primary_strategy == "section_609":
            return "section_611"
        return "fcra_violation"

    else:
        # Round 3+: Maximum pressure — FCRA violation letter + furnisher direct simultaneously
        return "fcra_violation"


LETTER_TYPE_DESCRIPTIONS = {
    "metro2_compliance": "Metro 2 Format Compliance Dispute — cites specific CDIA Metro 2 field violations",
    "section_609": "Section 609 Method of Verification Request — demands original documentation",
    "section_611": "Section 611 Reinvestigation Demand — demands re-investigation after failed verification",
    "fcra_violation": "FCRA Violation Notice — cites specific statutory violations with damages warning",
    "furnisher_direct": "Furnisher Direct Dispute — sent directly to data furnisher under § 1681s-2",
}
