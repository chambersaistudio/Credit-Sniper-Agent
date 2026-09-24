"""
Per-account dispute reasoning.

The model sees one canonical account — its current bureau records and the
deterministic findings about it, numbered F1..Fn — and nothing else (no
consumer identity, no other accounts). It must either tie a dispute ground
to specific findings or say plainly that there is none. Its answer is then
checked deterministically: findings, fields, recipients, and citations it
names must exist; a "dispute" that rests on no real finding is downgraded
to "need more evidence". The result is a proposal; nothing here opens a case.
"""
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.services.ai import ModelTier, generate
from app.services.credit_profile import RECORD_FIELDS, AccountView
from app.services.findings import Finding, Severity
from app.services.legal_references import REFERENCES, catalog_for_prompt
from app.services.redaction import Identity, redact

# A proposed dispute ground below this confidence gets a second, deeper look.
ESCALATION_CONFIDENCE = 0.5

SYSTEM_PROMPT = f"""You evaluate whether one consumer credit account has a legitimate basis for dispute \
under the Fair Credit Reporting Act. You are given that account's records from each bureau reporting it \
and a list of findings (F1, F2, ...) produced by deterministic rules.

Your job is accuracy, not volume. Most negative accounts are reported correctly; a negative item is not a \
dispute ground by itself, and a difference between bureaus is not automatically an error. Recommend a \
dispute only when specific findings show information that is inaccurate, incomplete, obsolete, or \
unverifiable — and say plainly when they don't.

Rules:
- Base every statement on the records and findings provided. Do not assume facts that aren't shown.
- A dispute ground must cite at least one finding id it rests on.
- Cite legal or reporting-standard references only by id from the catalog below, and only when they \
genuinely apply to the facts; explain why for each.
- Recipients: a bureau reporting the inaccuracy, the furnisher (the creditor or collector), or both. \
Only name bureaus that actually report this account.
- If the facts are ambiguous, say what additional evidence would settle it and lower your confidence.

Reference catalog:
{catalog_for_prompt()}"""

Recipient = Literal["equifax", "experian", "transunion", "furnisher"]
Action = Literal["dispute_bureau", "dispute_furnisher", "dispute_both", "no_dispute", "need_more_evidence"]


class LegalBasisOut(BaseModel):
    reference_id: str
    applies_because: str


class ClaimProposalOut(BaseModel):
    has_dispute_ground: bool
    reasoning: str = Field(description="Plain-language explanation for the consumer, grounded in the findings")
    supporting_finding_ids: list[str] = Field(description="Ids like F1 that the dispute ground rests on")
    disputed_fields: list[str]
    recipients: list[Recipient]
    legal_basis: list[LegalBasisOut]
    requested_remedy: str | None
    additional_evidence_needed: list[str]
    recommended_action: Action
    confidence: float = Field(description="0.0 to 1.0")


@dataclass
class ClaimProposal:
    has_dispute_ground: bool
    recommended_action: str
    reasoning: str
    supporting_findings: list[Finding]
    disputed_fields: list[str]
    recipients: list[str]
    legal_explanations: dict[str, str]
    requested_remedy: str | None
    additional_evidence_needed: list[str]
    confidence: float
    tier: ModelTier
    model: str
    validation_notes: list[str] = field(default_factory=list)


def build_prompt(view: AccountView, finding_ids: dict[str, Finding], identity: Identity | None = None) -> str:
    """Only this account's tradeline data. Free-text values pass through the
    same deterministic redaction as extraction, in case a remark or a
    misparsed field carries the consumer's identity."""

    def clean(value):
        return redact(value, identity).text if isinstance(value, str) else value

    records = [
        {k: clean(r.get(k)) for k in ("bureau", "as_of", *RECORD_FIELDS) if r.get(k) not in (None, "")}
        for r in view.records
    ]
    findings = [{"id": fid, **f.to_dict()} for fid, f in finding_ids.items()]
    return (
        f"<account>\nCreditor: {clean(view.canonical.creditor_name)}\nType: {view.canonical.account_type or 'unknown'}\n</account>\n"
        f"<bureau_records>\n{json.dumps(records, indent=2, default=str)}\n</bureau_records>\n"
        f"<findings>\n{json.dumps(findings, indent=2) if findings else 'None — the rules found no issues.'}\n</findings>\n\n"
        "Is there a legitimate dispute ground for this account?"
    )


def validate_proposal(out: ClaimProposalOut, view: AccountView, finding_ids: dict[str, Finding]) -> tuple[ClaimProposalOut, list[Finding], list[str]]:
    """Deterministic checks on the model's answer. Returns the corrected
    output, the findings it legitimately rests on, and notes on anything removed."""
    notes: list[str] = []
    supporting = [finding_ids[i] for i in out.supporting_finding_ids if i in finding_ids]
    if len(supporting) != len(out.supporting_finding_ids):
        notes.append("Dropped references to findings that don't exist.")

    reporting = {r["bureau"] for r in view.records}
    recipients = [r for r in dict.fromkeys(out.recipients) if r == "furnisher" or r in reporting]
    if len(recipients) != len(set(out.recipients)):
        notes.append("Dropped recipient bureaus that don't report this account.")

    legal = [b for b in out.legal_basis if b.reference_id in REFERENCES]
    if len(legal) != len(out.legal_basis):
        notes.append("Dropped citations not in the vetted reference catalog.")

    fields = [f for f in out.disputed_fields if f in RECORD_FIELDS]
    confidence = min(max(out.confidence, 0.0), 1.0)

    corrected = out.model_copy(update={
        "supporting_finding_ids": [i for i in out.supporting_finding_ids if i in finding_ids],
        "recipients": recipients, "legal_basis": legal, "disputed_fields": fields, "confidence": confidence,
    })
    if corrected.has_dispute_ground and (not supporting or not recipients):
        notes.append("A dispute ground must rest on at least one real finding and name a valid recipient.")
        corrected = corrected.model_copy(update={
            "has_dispute_ground": False, "recommended_action": "need_more_evidence", "recipients": [],
        })
    if not corrected.has_dispute_ground and corrected.recommended_action not in ("no_dispute", "need_more_evidence"):
        corrected = corrected.model_copy(update={"recommended_action": "no_dispute"})
    # "No dispute ground" must mean the records were sufficient to judge the
    # account accurate — never that information was missing. If the model
    # itself is still asking for more evidence, that's "need more evidence",
    # not a clean "no dispute". Prevents concluding no-dispute from a gap.
    if corrected.recommended_action == "no_dispute" and corrected.additional_evidence_needed:
        corrected = corrected.model_copy(update={"recommended_action": "need_more_evidence"})
        notes.append("Model still requested more evidence, so this is 'need more evidence', not 'no dispute'.")
    return corrected, supporting, notes


def _needs_escalation(out: ClaimProposalOut, finding_ids: dict[str, Finding]) -> bool:
    if out.has_dispute_ground and out.confidence < ESCALATION_CONFIDENCE:
        return True
    # The rules call something a supported ground but the model sees none:
    # conflicting signals deserve the deeper look.
    strong = any(f.severity == Severity.SUPPORTED_DISPUTE_GROUND for f in finding_ids.values())
    return strong and not out.has_dispute_ground


async def evaluate_account(
    view: AccountView, context: dict[str, Any] | None = None, identity: Identity | None = None
) -> ClaimProposal:
    finding_ids = {f"F{i}": f for i, f in enumerate(view.findings, start=1)}
    prompt = build_prompt(view, finding_ids, identity)

    tier = ModelTier.REASONING
    generation = await generate(
        tier, system=SYSTEM_PROMPT, prompt=prompt, output_type=ClaimProposalOut,
        task="evaluate_account", context=context,
    )
    if _needs_escalation(generation.output, finding_ids):
        tier = ModelTier.ESCALATION
        generation = await generate(
            tier, system=SYSTEM_PROMPT, prompt=prompt, output_type=ClaimProposalOut,
            task="evaluate_account_escalated", context=context,
        )

    out, supporting, notes = validate_proposal(generation.output, view, finding_ids)
    return ClaimProposal(
        has_dispute_ground=out.has_dispute_ground,
        recommended_action=out.recommended_action,
        reasoning=out.reasoning,
        supporting_findings=supporting,
        disputed_fields=out.disputed_fields,
        recipients=out.recipients,
        legal_explanations={b.reference_id: b.applies_because for b in out.legal_basis},
        requested_remedy=out.requested_remedy,
        additional_evidence_needed=out.additional_evidence_needed,
        confidence=out.confidence,
        tier=tier,
        model=generation.model,
        validation_notes=notes,
    )
