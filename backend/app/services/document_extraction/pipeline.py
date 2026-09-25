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
from app.services.document_extraction.status import OPERATIONAL_REASONS, ExtractionStatus

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
- Keep these distinct concepts in their own fields, never merged:
  * `balance_updated` is the "Balance updated" date. `date_last_reported` is only for a field the report \
actually labels "Last reported"/"Date reported". If only "Balance updated" is printed, leave \
`date_last_reported` null.
  * `payment_status` is the account's own payment standing. A page or section label such as "Potentially \
negative" or "Exceptional payment history" is a `report_classification`, not a payment status or account status.
  * For inquiries, `inquiry_type` is hard/soft and ONLY when the document says which — an inquiry being \
listed is not evidence that it is hard. `inquiry_category` records why it happened, taken from the section \
heading: TransUnion's "Promotional Inquiries" and "Account Review Inquiries" sections are `promotional` and \
`account_review`, which those disclosures describe as visible only to the consumer and not affecting the \
score, so they are `soft`. A "Business Type" such as "Bank Credit Cards" is the company's industry and \
belongs in `business_type`.
  * `document_created_date` is when the document was produced ("Date Created"). \
`consumer_on_file_since` is how long the bureau has had a file on this consumer — historical metadata, \
never the date of this report. Keep them apart, and don't put either in the other.
  * A monthly payment grid may print money and remarks per month as well as a status letter. Capture \
`balance`, `past_due`, `amount_paid`, `amount_due` and `remarks` for a month whenever they are printed.
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

Field semantics you must respect rather than "correct":
- `balance_updated` and `date_last_reported` are different fields. A "Balance updated" date belongs in \
`balance_updated`, and `date_last_reported` staying null is correct when the report labels no "Last reported" field.
- A page/section label such as "Potentially negative" or "Exceptional payment history" belongs in \
`report_classification`. It is not a `payment_status` or an account status.
- An inquiry's `inquiry_type` is hard/soft. A "Business Type" such as "Bank Credit Cards" is the company's \
industry and belongs in `business_type` — do not report it as a wrong `inquiry_type`.
- A field the report simply does not print (for example no Date of First Delinquency in a consumer \
disclosure) is not an extraction error. Report it, if at all, as `ambiguous`, never as a `correction`.

Report every genuine disagreement as a finding. Set `verified` true only if the extraction can be relied on as-is."""


@dataclass
class DocumentExtractionResult:
    extraction: CreditReportExtraction | None
    audit: AuditReport | None
    status: ExtractionStatus
    reasons: list[str] = field(default_factory=list)
    extractor_model: str | None = None
    auditor_model: str | None = None
    # Operator detail: the provider's own error. Recorded for logs and admin
    # telemetry and deliberately never surfaced in a consumer-facing response.
    provider_error: str | None = None
    # The classified failure, when a pass failed. Operator-only, like the above.
    failure: "PassFailure | None" = None

    def audit_to_dict(self) -> dict[str, Any]:
        """What we persist about the audit — findings and counts, never the
        document or the model's view of its contents beyond the findings."""
        benign, _ = triage_findings(self.extraction, self.audit) if self.extraction else ([], [])
        return {
            "status": self.status.value,
            "reasons": self.reasons,
            "extractor_model": self.extractor_model,
            "auditor_model": self.auditor_model,
            "audit": self.audit.model_dump() if self.audit else None,
            # Kept visible, but they did not hold the report back.
            "set_aside": [{**f.model_dump(), "set_aside_because": why} for f, why in benign],
            # Operator-only; the report endpoints never return these fields.
            "provider_error": self.provider_error,
            "failure": self.failure.to_dict() if self.failure else None,
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


# Page/section labels Experian files accounts under. They describe where an
# account sits in the report, not how it is being paid.
_REPORT_CLASSIFICATIONS = (
    "potentially negative", "exceptional payment history", "accounts in good standing",
    "negative items", "closed accounts", "open accounts",
)
_NOT_STATED = ("", "none", "null", "n/a", "na", "not disclosed", "not reported", "not provided", "-")


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def _benign_reason(finding, extraction: CreditReportExtraction) -> str | None:
    """Is this audit finding a known field-semantics confusion rather than a
    real disagreement about the document?

    These are cases where the auditor reads a value correctly but files it
    under the wrong concept. Treating them as blocking would hold a perfectly
    good extraction hostage, so they are recorded and set aside. Anything not
    matched here still blocks."""
    field = _norm(finding.field)
    proposed = _norm(finding.correct_value)

    # "Business Type: Bank Credit Cards" is the company's industry, not the
    # inquiry's hard/soft classification.
    if field in ("inquiry_type", "business_type"):
        known_business_types = {_norm(i.business_type) for i in extraction.inquiries if i.business_type}
        if proposed and (proposed not in ("hard", "soft") or field == "business_type"):
            return "Business Type is the company's industry, not the inquiry's hard/soft type."
        if proposed in known_business_types:
            return "Proposed inquiry_type is a Business Type already recorded in business_type."

    # "Balance updated" is its own date; date_last_reported stays null unless
    # the report labels a Last reported / Date reported field.
    if field in ("date_last_reported", "balance_updated"):
        balance_updates = {_norm(a.balance_updated) for a in extraction.accounts if a.balance_updated}
        if proposed and proposed in balance_updates:
            return "'Balance updated' is a distinct field from 'Last reported'; it is recorded separately."

    # A page-level label is not a payment or account status.
    if field in ("payment_status", "status_raw", "status_normalized", "account_status"):
        classifications = {_norm(a.report_classification) for a in extraction.accounts if a.report_classification}
        if proposed in classifications or any(c in proposed for c in _REPORT_CLASSIFICATIONS):
            return "Report section label recorded as report_classification, not a payment/account status."

    # A field the disclosure simply doesn't print is not an extraction error.
    if "delinquency" in field and proposed in _NOT_STATED:
        return "Date of First Delinquency is not disclosed in this report; absence is not an inaccuracy."

    return None


def triage_findings(extraction: CreditReportExtraction, audit: AuditReport | None):
    """Split audit findings into (benign field-semantics confusions, blocking
    disagreements)."""
    if audit is None:
        return [], []
    benign, blocking = [], []
    for finding in audit.findings:
        reason = _benign_reason(finding, extraction)
        (benign if reason else blocking).append((finding, reason))
    return benign, blocking


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

    benign, blocking = triage_findings(extraction, audit)
    blocking_findings = [f for f, _ in blocking]
    # A disagreement explained entirely by field-semantics confusion doesn't
    # hold the report back; an unexplained "not verified" still does.
    fully_explained = bool(benign) and not blocking_findings

    if not audit.account_count_matches:
        counted = audit.account_count_in_document
        reasons.append(
            f"Audit counted {counted} tradelines in the document but the extraction has {len(extraction.accounts)}."
        )
    if not audit.identities_correct:
        reasons.append("Audit disputes one or more account identities (furnisher vs original creditor).")
    if not audit.inquiries_correct and not fully_explained:
        reasons.append("Audit disputes the extracted inquiries.")

    missing = [f for f in blocking_findings if f.kind == "missing_account"]
    if missing:
        reasons.append(f"Audit found {len(missing)} account(s) missing from the extraction.")
    duplicated = [f for f in blocking_findings if f.kind == "duplicate_account"]
    if duplicated:
        reasons.append(f"Audit found {len(duplicated)} duplicated account(s).")
    corrections = [f for f in blocking_findings if f.kind == "correction"]
    if corrections:
        reasons.append(f"Audit corrected {len(corrections)} field(s); values were not auto-applied.")
    unsupported = [f for f in blocking_findings if f.kind == "unsupported_field"]
    if unsupported:
        reasons.append(f"Audit found {len(unsupported)} field(s) the document does not support.")
    ambiguous = [f for f in blocking_findings if f.kind == "ambiguous"]
    if ambiguous:
        reasons.append(f"Audit flagged {len(ambiguous)} ambiguity/ambiguities needing review.")

    # Missing or duplicated accounts mean we don't have the document's real
    # contents — that's incomplete, not merely unverified.
    if missing or duplicated or duplicates or not audit.account_count_matches or not extraction.accounts:
        return ExtractionStatus.EXTRACTION_INCOMPLETE, reasons
    if reasons:
        return ExtractionStatus.NEEDS_AUDIT, reasons
    if not audit.verified and not fully_explained:
        return ExtractionStatus.NEEDS_AUDIT, ["Audit did not verify the extraction."]
    return ExtractionStatus.VERIFIED, []


@dataclass
class PassFailure:
    """One failed expensive pass, kept in operator vocabulary.

    Everything here is operator-only. The consumer sees copy chosen from the
    status alone (ExtractionStatus.OPERATIONAL_MESSAGES); none of these
    fields — provider text, token counts, response ids — is ever returned by
    a consumer-facing endpoint."""

    pass_name: str
    status: ExtractionStatus
    error_class: str
    detail: str
    # Populated when the provider billed us anyway.
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int | None = None
    max_tokens: int | None = None
    latency_ms: float = 0.0
    response_id: str | None = None

    @classmethod
    def from_error(cls, pass_name: str, error: AIError) -> "PassFailure":
        usage = getattr(error, "usage", None)
        return cls(
            pass_name=pass_name,
            status=ExtractionStatus.for_error(error),
            error_class=type(error).__name__,
            detail=str(error),
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            reasoning_tokens=usage.reasoning_tokens if usage else None,
            max_tokens=usage.max_tokens if usage else None,
            latency_ms=usage.latency_ms if usage else 0.0,
            response_id=usage.response_id if usage else None,
        )

    @property
    def billed(self) -> bool:
        return bool(self.input_tokens or self.output_tokens)

    @property
    def cost_note(self) -> str:
        if not self.billed:
            return " (nothing billed)"
        reasoning = f", {self.reasoning_tokens} reasoning" if self.reasoning_tokens else ""
        return (f" (BILLED: {self.input_tokens} in / {self.output_tokens} out{reasoning}"
                f"{f', budget {self.max_tokens}' if self.max_tokens else ''})")

    # Kept on the report for operators; never returned to a consumer.
    def to_dict(self) -> dict[str, Any]:
        return {
            "pass": self.pass_name, "status": self.status.value, "error_class": self.error_class,
            "detail": self.detail, "billed": self.billed,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens, "max_output_tokens": self.max_tokens,
            "latency_ms": round(self.latency_ms, 1), "response_id": self.response_id,
        }


async def run_extractor(
    document: bytes, *, filename: str = "credit-report.pdf", context: dict[str, Any] | None = None
) -> tuple[CreditReportExtraction | None, str | None, "PassFailure | None"]:
    """Pass 1 on its own. Returns (extraction, model, failure).

    The failure is classified rather than flattened: a provider outage and a
    response that blew the token budget are different events with different
    costs, and only one of them is worth retrying."""
    try:
        generation = await generate_document(
            ModelTier.DOCUMENT_EXTRACTION,
            system=EXTRACTOR_SYSTEM,
            prompt=_extraction_prompt(),
            document=document,
            filename=filename,
            output_type=CreditReportExtraction,
            task="extract_report_document",
            context=context or {},
            detail=settings.document_extraction_detail,
        )
    except AIError as e:
        failure = PassFailure.from_error("extract", e)
        logger.warning("Document extraction failed (%s -> %s): %s%s",
                       type(e).__name__, failure.status.value, e, failure.cost_note)
        return None, None, failure
    return generation.output, generation.model, None


async def run_auditor(
    document: bytes, extraction: CreditReportExtraction, *,
    filename: str = "credit-report.pdf", context: dict[str, Any] | None = None,
) -> tuple[AuditReport | None, str | None, "PassFailure | None"]:
    """Pass 2 on its own, so a failed audit never re-runs the extractor."""
    try:
        generation = await generate_document(
            ModelTier.DOCUMENT_AUDIT,
            system=AUDITOR_SYSTEM,
            prompt=_audit_prompt(extraction),
            document=document,
            filename=filename,
            output_type=AuditReport,
            task="audit_report_document",
            context=context or {},
            detail=settings.document_extraction_detail,
        )
    except AIError as e:
        failure = PassFailure.from_error("audit", e)
        logger.warning("Document audit failed (%s -> %s): %s%s",
                       type(e).__name__, failure.status.value, e, failure.cost_note)
        return None, None, failure
    return generation.output, generation.model, None


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
        # The provider failed us; we never read the document. Nothing is known
        # about the report's contents, so this must not be reported as a
        # problem with the consumer's PDF. The provider's own message goes to
        # the logs and the audit record, never to the consumer.
        failure = PassFailure.from_error("extract", e)
        logger.warning("Document extraction failed (%s -> %s): %s%s",
                       type(e).__name__, failure.status.value, e, failure.cost_note)
        return DocumentExtractionResult(
            None, None, failure.status,
            [OPERATIONAL_REASONS[failure.status]],
            provider_error=f"{type(e).__name__}: {e}",
            failure=failure,
        )

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
