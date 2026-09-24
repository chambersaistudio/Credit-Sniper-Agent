"""
Two-pass, AI-native document ingestion.

    original PDF bytes ─▶ PASS 1 extractor  ─▶ structured extraction
                       └▶ PASS 2 auditor    ─▶ audit of that extraction
                                            └▶ deterministic reconciliation
                                               ─▶ explicit ExtractionStatus

Both passes read the SAME original PDF. Nothing local pre-digests the
document for them: the live Experian failure happened because a regex parser
misunderstood the layout before any model saw it.

Reconciliation is deterministic and conservative. The auditor's corrections
are recorded as findings, never silently applied — two models disagreeing
about a consumer's credit file is a reason to withhold the report from
dispute analysis, not to average them.
"""
import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.services.ai import AIError, ModelTier, generate_document
from app.services.document_extraction.schema import AuditReport, CreditReportExtraction
from app.services.document_extraction.status import ExtractionStatus

logger = logging.getLogger(__name__)

EXTRACTOR_SYSTEM = """You read consumer credit report PDFs and transcribe them into structured data.

You are looking at the original report. Read every page, including two-column grids, account tables and \
payment-history charts.

Rules:
- Transcribe only what the document shows. If a field is not printed for an account, return null. Never infer, \
calculate, or carry a value over from another account.
- The account's own name is the company REPORTING it — the furnisher, or the collection agency for a \
collection. If the report also names an original creditor, that goes in `original_creditor`, never in \
`creditor_name`. A collection listed by "Caine & Weiner" with "Original creditor: Progressive" is the account \
"Caine & Weiner" with original creditor "Progressive" — it is NOT an account named "Progressive".
- Copy money and dates exactly as printed (for example "$1,204" and "Feb 15, 2026"). Do not convert them.
- Transcribe the month-by-month payment grid cell by cell, keeping each code exactly as printed.
- Include every tradeline in the report, including closed and collection accounts, and do not list the same \
tradeline twice.
- For each account record the pages you read it from, a short excerpt proving its identity, and short excerpts \
for the important fields."""

AUDITOR_SYSTEM = """You audit a structured extraction against the original credit report PDF.

You are NOT doing credit analysis and NOT deciding whether anything is disputable. Your only job is whether \
the extraction faithfully reflects the document.

Check, against the PDF itself:
- the number of tradelines, and whether any are missing or duplicated
- each account's identity: is it named after the company reporting it, with any original creditor recorded \
separately and correctly?
- account numbers, statuses, amounts, dates, payment histories
- the inquiries and their dates
- any field asserted by the extraction that the document does not support

Report every disagreement as a finding. Set `verified` true only if the extraction can be relied on as-is."""


@dataclass
class DocumentExtractionResult:
    extraction: CreditReportExtraction | None
    audit: AuditReport | None
    status: ExtractionStatus
    reasons: list[str] = field(default_factory=list)
    extractor_model: str | None = None
    auditor_model: str | None = None

    def audit_to_dict(self) -> dict[str, Any]:
        """What we persist about the audit — findings and counts, never the
        document or the model's view of its contents beyond the findings."""
        return {
            "status": self.status.value,
            "reasons": self.reasons,
            "extractor_model": self.extractor_model,
            "auditor_model": self.auditor_model,
            "audit": self.audit.model_dump() if self.audit else None,
        }


def _extraction_prompt() -> str:
    return (
        "Transcribe this credit report completely and exactly into the required structure. "
        "Return null for anything the document does not state."
    )


def _audit_prompt(extraction: CreditReportExtraction) -> str:
    return (
        "Here is a structured extraction of the attached credit report, produced by another pass.\n\n"
        f"<extraction>\n{extraction.model_dump_json(indent=2)}\n</extraction>\n\n"
        "Audit it against the attached original PDF and report every disagreement."
    )


def reconcile(extraction: CreditReportExtraction, audit: AuditReport | None) -> tuple[ExtractionStatus, list[str]]:
    """Deterministic gate over the two passes. Conservative by construction:
    anything unresolved keeps the report out of dispute analysis."""
    reasons: list[str] = []

    if not extraction.accounts:
        return ExtractionStatus.EXTRACTION_INCOMPLETE, ["No tradelines were extracted from the document."]

    # Structural problems the extractor itself reported.
    if extraction.unreadable_pages:
        reasons.append(f"Pages could not be read reliably: {sorted(extraction.unreadable_pages)}.")
    # An account with no identity evidence isn't auditable later.
    unsourced = [a.creditor_name for a in extraction.accounts if not a.source_pages]
    if unsourced:
        reasons.append(f"{len(unsourced)} account(s) have no source page recorded.")

    # Duplicate tradelines: same creditor AND same account number.
    seen: set[tuple[str, str]] = set()
    duplicates: list[str] = []
    for account in extraction.accounts:
        if not account.account_number:
            continue
        key = (account.creditor_name.strip().lower(), account.account_number.strip().lower())
        if key in seen:
            duplicates.append(account.creditor_name)
        seen.add(key)
    if duplicates:
        reasons.append(f"Duplicate tradelines extracted: {', '.join(sorted(set(duplicates)))}.")

    if audit is None:
        reasons.append("No independent audit was run against the document.")
        return ExtractionStatus.NEEDS_AUDIT, reasons

    if not audit.account_count_matches:
        counted = audit.account_count_in_document
        reasons.append(
            f"Audit counted {counted} tradelines in the document but the extraction has {len(extraction.accounts)}."
        )
    if not audit.identities_correct:
        reasons.append("Audit disputes one or more account identities (furnisher vs original creditor).")
    if not audit.inquiries_correct:
        reasons.append("Audit disputes the extracted inquiries.")

    missing = [f for f in audit.findings if f.kind == "missing_account"]
    if missing:
        reasons.append(f"Audit found {len(missing)} account(s) missing from the extraction.")
    duplicated = [f for f in audit.findings if f.kind == "duplicate_account"]
    if duplicated:
        reasons.append(f"Audit found {len(duplicated)} duplicated account(s).")
    corrections = [f for f in audit.findings if f.kind == "correction"]
    if corrections:
        reasons.append(f"Audit corrected {len(corrections)} field(s); values were not auto-applied.")
    unsupported = [f for f in audit.findings if f.kind == "unsupported_field"]
    if unsupported:
        reasons.append(f"Audit found {len(unsupported)} field(s) the document does not support.")
    ambiguous = [f for f in audit.findings if f.kind == "ambiguous"]
    if ambiguous:
        reasons.append(f"Audit flagged {len(ambiguous)} ambiguity/ambiguities needing review.")

    # Missing or duplicated accounts mean we don't have the document's real
    # contents — that's incomplete, not merely unverified.
    if missing or duplicated or duplicates or not audit.account_count_matches or not extraction.accounts:
        return ExtractionStatus.EXTRACTION_INCOMPLETE, reasons
    if not audit.verified or reasons:
        return ExtractionStatus.NEEDS_AUDIT, reasons or ["Audit did not verify the extraction."]
    return ExtractionStatus.VERIFIED, []


async def extract_document(
    document: bytes, *, filename: str = "credit-report.pdf", context: dict[str, Any] | None = None
) -> DocumentExtractionResult:
    """Run both passes over the original PDF and return the gated result.

    Raises nothing on provider failure: a failed read is a FAILED status, not
    an exception that loses the upload."""
    context = context or {}
    try:
        first = await generate_document(
            ModelTier.DOCUMENT_EXTRACTION,
            system=EXTRACTOR_SYSTEM,
            prompt=_extraction_prompt(),
            document=document,
            filename=filename,
            output_type=CreditReportExtraction,
            task="extract_report_document",
            context=context,
            detail=settings.document_extraction_detail,
        )
    except AIError as e:
        # Message only — never the document or the request body.
        logger.warning("Document extraction failed: %s", type(e).__name__)
        return DocumentExtractionResult(None, None, ExtractionStatus.FAILED, [f"Document extraction failed: {e}"])

    extraction = first.output
    audit = None
    auditor_model = None
    if settings.document_audit_enabled:
        try:
            second = await generate_document(
                ModelTier.DOCUMENT_AUDIT,
                system=AUDITOR_SYSTEM,
                prompt=_audit_prompt(extraction),
                document=document,
                filename=filename,
                output_type=AuditReport,
                task="audit_report_document",
                context=context,
                detail=settings.document_extraction_detail,
            )
            audit = second.output
            auditor_model = second.model
        except AIError as e:
            logger.warning("Document audit failed: %s", type(e).__name__)

    status, reasons = reconcile(extraction, audit)
    return DocumentExtractionResult(
        extraction=extraction, audit=audit, status=status, reasons=reasons,
        extractor_model=first.model, auditor_model=auditor_model,
    )
