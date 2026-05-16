"""
AI-powered credit report analysis engine.
Uses Claude to identify FCRA violations, Metro 2 inconsistencies,
and categorize every disputable item with a specific strategy.
"""
import json
import logging
from typing import Any

import anthropic

from app.config import settings

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

ANALYSIS_SYSTEM_PROMPT = """You are an expert credit analyst and FCRA attorney with deep knowledge of:
- Fair Credit Reporting Act (FCRA), 15 U.S.C. § 1681 et seq.
- Metro 2 Format (Consumer Data Industry Association standard for credit reporting)
- e-OSCAR dispute processing system and what triggers human review vs automated rejection
- All three major credit bureaus' dispute processes (Equifax, Experian, TransUnion)
- Successful credit dispute strategies used by top credit repair professionals

Your task is to analyze credit report data and identify every single disputable item with maximum specificity.

For each disputable item, you must identify:
1. The EXACT violation (FCRA section, Metro 2 field code, or factual error)
2. The specific dispute strategy most likely to succeed
3. Priority (1-10, where 10 = most impactful/easiest to win)
4. Estimated score impact if removed
5. Whether to dispute with bureau, furnisher directly, or both

Metro 2 violation codes to check:
- Field 17A (Account Status Code) - incorrect status codes
- Field 17B (Payment Rating) - inaccurate payment ratings
- Field 18 (Payment History Profile) - 24-month history errors
- Field 20 (Scheduled Monthly Payment Amount) - incorrect amounts
- Field 23 (Amount Past Due) - inaccurate past due amounts
- Field 26 (Date of First Delinquency) - incorrect DOFD (most commonly exploited)
- Field 27 (Date Closed) - missing or inaccurate closed dates
- Field 35 (Compliance Condition Code) - missing required codes
- Field 36 (Original Charge-Off Amount) - inaccurate charge-off amounts

FCRA violations to check:
- 15 U.S.C. § 1681c - obsolete information (7/10 year limits)
- 15 U.S.C. § 1681e(b) - maximum possible accuracy requirement
- 15 U.S.C. § 1681i - reinvestigation requirements
- 15 U.S.C. § 1681s-2 - furnisher duties of accuracy
- 15 U.S.C. § 1681n - willful noncompliance
- 15 U.S.C. § 1681o - negligent noncompliance

Common dispute angles that WIN:
1. Date of First Delinquency errors (very common, resets 7-year clock incorrectly)
2. Balance inaccuracies (any difference, even $1, is a violation)
3. Account reported as open when legally closed
4. Charge-off still reporting balance when sold to collector (double-jeopardy)
5. Inquiries without permissible purpose
6. Unverifiable accounts (furnisher no longer exists, account was sold multiple times)
7. Re-aged debts (DOFD manipulated to extend reporting period)
8. Inconsistencies between bureaus (same account, different data)

Always respond in valid JSON format."""


ANALYSIS_USER_PROMPT = """Analyze this credit report data and identify ALL disputable items.

Credit Report Data:
{report_data}

Return a JSON object with this exact structure:
{{
  "summary": {{
    "total_accounts": <number>,
    "disputable_accounts": <number>,
    "total_inquiries": <number>,
    "disputable_inquiries": <number>,
    "estimated_score_gain": <number>,
    "highest_priority_items": [<list of creditor names>],
    "overall_strategy": "<description of overall dispute strategy>"
  }},
  "disputable_accounts": [
    {{
      "creditor_name": "<name>",
      "account_number": "<masked number>",
      "is_disputable": true,
      "priority_score": <1-10>,
      "estimated_score_impact": <points>,
      "dispute_type": "<bureau_dispute|furnisher_direct|both>",
      "primary_strategy": "<metro2_compliance|section_609|section_611|fcra_violation|factual_dispute>",
      "violations": [
        {{
          "violation_type": "<metro2|fcra|factual>",
          "specific_violation": "<exact Metro 2 field or FCRA section>",
          "description": "<what is wrong>",
          "evidence": "<what in the report data supports this>",
          "legal_citation": "<exact legal citation>"
        }}
      ],
      "dispute_reasons": ["<reason 1>", "<reason 2>"],
      "recommended_letter_type": "<letter type>",
      "recommended_recipient": "<bureau name or furnisher>",
      "notes": "<any additional strategic notes>"
    }}
  ],
  "disputable_inquiries": [
    {{
      "creditor_name": "<name>",
      "inquiry_date": "<date>",
      "is_disputable": true,
      "dispute_reason": "<reason>",
      "permissible_purpose_question": "<why this may lack permissible purpose>",
      "legal_citation": "15 U.S.C. § 1681b"
    }}
  ],
  "non_disputable_accounts": [
    {{
      "creditor_name": "<name>",
      "reason_not_disputed": "<why>"
    }}
  ]
}}"""


def analyze_credit_report(parsed_data: dict[str, Any]) -> dict[str, Any]:
    """
    Run AI analysis on parsed credit report data.
    Returns structured analysis with all disputable items.
    """
    report_summary = {
        "bureau": parsed_data.get("bureau"),
        "credit_score": parsed_data.get("credit_score"),
        "report_date": parsed_data.get("report_date"),
        "accounts": parsed_data.get("accounts_raw", []),
        "inquiries": parsed_data.get("inquiries_raw", []),
        "personal_info": parsed_data.get("personal_info", {}),
    }

    # If raw extraction got no accounts, send raw text for AI to parse
    if (
        not report_summary["accounts"]
        or (len(report_summary["accounts"]) == 1
            and report_summary["accounts"][0].get("extraction_method") == "full_text_ai_parse")
    ):
        report_summary["raw_text"] = parsed_data.get("raw_text", "")[:15000]

    report_data_str = json.dumps(report_summary, indent=2)

    # Truncate if too large
    if len(report_data_str) > 30000:
        report_data_str = report_data_str[:30000] + "\n... [truncated for length]"

    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=8000,
        system=ANALYSIS_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": ANALYSIS_USER_PROMPT.format(report_data=report_data_str),
            }
        ],
    )

    raw_response = response.content[0].text

    # Strip markdown code fences if present
    raw_response = raw_response.strip()
    if raw_response.startswith("```"):
        raw_response = raw_response.split("```", 2)[1]
        if raw_response.startswith("json"):
            raw_response = raw_response[4:]
        raw_response = raw_response.rsplit("```", 1)[0]

    analysis = json.loads(raw_response.strip())
    return analysis


def generate_dispute_strategy(
    account_data: dict[str, Any],
    analysis_result: dict[str, Any],
    round_number: int = 1,
    previous_response: str | None = None,
) -> dict[str, Any]:
    """
    Generate specific dispute strategy for a single account,
    considering round number and any previous bureau response.
    """
    strategy_prompt = f"""Based on this account analysis and dispute history, provide the optimal Round {round_number} dispute strategy.

Account Data:
{json.dumps(account_data, indent=2)}

Analysis Result:
{json.dumps(analysis_result, indent=2)}

Round Number: {round_number}
Previous Bureau Response: {previous_response or "None (first round)"}

Provide a JSON response with:
{{
  "round": {round_number},
  "primary_angle": "<the main dispute angle for this round>",
  "letter_type": "<letter type>",
  "key_arguments": ["<argument 1>", "<argument 2>"],
  "legal_citations": ["<citation 1>", "<citation 2>"],
  "escalation_from_previous": "<how this escalates from previous round if applicable>",
  "furnisher_dispute_needed": <true|false>,
  "estimated_success_probability": "<percentage>",
  "timing_notes": "<any specific timing considerations>"
}}"""

    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=2000,
        messages=[{"role": "user", "content": strategy_prompt}],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0]

    return json.loads(raw.strip())
