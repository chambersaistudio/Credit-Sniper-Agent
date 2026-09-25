"""
Canonical credit-report extraction schema.

This is the shape of a credit report as a *document*, not the shape our old
regex parser happened to support. It is provider-neutral: the document
provider fills it via strict structured outputs, and nothing here references
a vendor.

Two rules the schema enforces by construction:

* The reporting furnisher/collector (`creditor_name`), the `original_creditor`
  and the company an account was `sold_to` are three DIFFERENT fields. The
  live Experian failure reported six collections under their original
  creditors' names; keeping them separate makes that misreading impossible to
  express.
* Money and dates stay as the strings the report prints. The model transcribes;
  conversion to numbers/dates happens deterministically on our side, so the
  model never arithmetically "helps".

Every value is nullable and required to be present: strict structured outputs
demand all keys, and a missing value must come back as null rather than a
guess.
"""
from pydantic import BaseModel, Field


class FieldEvidence(BaseModel):
    """Why one extracted value exists: where it was read and the words read."""

    field: str = Field(description="Field name this evidence supports, e.g. 'balance'")
    value: str | None = Field(description="The value as printed in the document")
    page: int | None = Field(description="1-based page number the value appears on")
    excerpt: str | None = Field(description="Short verbatim excerpt containing the value")


class PaymentHistoryEntry(BaseModel):
    """One cell of the month-by-month payment grid, kept as reported.

    Some bureaus (TransUnion especially) print money and remarks per month,
    not just a status letter. Those are captured too — they are the raw
    material for later forensic analysis and must not be dropped.
    """

    year: int
    month: int = Field(description="1-12")
    raw_status_code: str = Field(description="The code exactly as printed, e.g. 'OK', '30', 'CO', 'ND'")
    status_code: str | None = Field(description="Normalized code if obvious, else null")
    balance: str | None = Field(description="Balance reported for this month, as printed")
    past_due: str | None = Field(description="Past-due amount reported for this month, as printed")
    amount_paid: str | None = Field(description="Amount paid in this month, as printed")
    amount_due: str | None = Field(description="Scheduled amount due for this month, as printed")
    remarks: list[str] = Field(description="Any remarks printed against this month")
    source_page: int | None = Field(description="1-based page this month's cell was read from")


class ContactInfo(BaseModel):
    name: str | None
    address: str | None
    phone: str | None


class ExtractedTradeline(BaseModel):
    creditor_name: str = Field(
        description="The company actually reporting this tradeline — the furnisher or collection agency "
                    "named as the account, NOT the original creditor"
    )
    original_creditor: str | None = Field(
        description="Original creditor when the report names one (typical for collections). Null otherwise."
    )
    sold_to: str | None = Field(description="Company the account was sold or transferred to, if stated")
    account_number: str | None = Field(description="Masked account number exactly as printed")
    account_type: str | None
    open_closed: str | None = Field(description="Open/closed state as reported")
    status_raw: str | None = Field(description="Account status exactly as worded in the report")
    status_normalized: str | None = Field(
        description="One of: open, closed, paid, charged_off, collection, transferred, sold, current, "
                    "derogatory, late — or null if unclear"
    )
    payment_status: str | None = Field(
        description="The account's own payment standing, e.g. 'Current', '30 days late'. NOT a page-level "
                    "classification like 'Potentially negative' or 'Exceptional payment history'."
    )
    report_classification: str | None = Field(
        description="Any page/section label the report files this account under, e.g. 'Potentially negative' "
                    "or 'Exceptional payment history'. Never put this in payment_status or status_raw."
    )
    balance: str | None
    balance_updated: str | None = Field(
        description="The 'Balance updated' date. This is when the balance was refreshed — it is NOT the "
                    "same as a 'Last reported'/'Date reported' field and must not be used as one."
    )
    credit_limit: str | None
    original_amount: str | None = Field(description="Original balance / original loan amount")
    past_due_amount: str | None
    monthly_payment: str | None
    high_balance: str | None
    terms: str | None
    responsibility: str | None
    date_opened: str | None
    date_closed: str | None
    status_updated: str | None
    date_first_delinquency: str | None = Field(description="Only if explicitly printed; never inferred")
    date_last_reported: str | None = Field(
        description="Only a field the report actually labels 'Last reported' / 'Date reported'. "
                    "If the report only shows 'Balance updated', leave this null."
    )
    date_last_payment: str | None
    remarks: str | None = Field(description="Remarks/comments printed for this account")
    consumer_dispute: str | None = Field(description="Consumer dispute notation, if the report shows one")
    contact: ContactInfo | None = Field(description="Furnisher/collector contact details when printed")
    payment_history: list[PaymentHistoryEntry]
    source_pages: list[int] = Field(description="1-based pages this tradeline was read from")
    identity_evidence: str | None = Field(
        description="Short verbatim excerpt of the account heading proving this tradeline's identity"
    )
    field_evidence: list[FieldEvidence] = Field(
        description="Evidence for the important fields: account name, original creditor, account number, "
                    "balance, past due, credit limit/original amount, status, and dates"
    )


class ExtractedInquiry(BaseModel):
    creditor_name: str = Field(description="The company that made the inquiry")
    inquiry_date: str | None
    inquiry_type: str | None = Field(
        description="'hard' or 'soft', ONLY when the document says which it is (for example by placing the "
                    "inquiry in a section it describes as affecting, or not affecting, the credit score). "
                    "Return null when the document doesn't say — never assume an inquiry is hard just "
                    "because it is listed. This is NOT the company's industry."
    )
    inquiry_category: str | None = Field(
        description="Why the inquiry happened, from the section it is printed under. One of: "
                    "credit_application, promotional, account_review, credit_monitoring, consumer_request, "
                    "insurance, employment, collection, other. TransUnion's 'Promotional Inquiries' and "
                    "'Account Review Inquiries' sections map to promotional and account_review."
    )
    business_type: str | None = Field(
        description="The company's industry as the report labels it, e.g. a 'Business Type' of "
                    "'Bank Credit Cards'. A different concept from inquiry_type — never put it there."
    )
    contact: ContactInfo | None
    source_pages: list[int]


class PublicRecord(BaseModel):
    record_type: str | None
    status: str | None
    filed_date: str | None
    amount: str | None
    reference: str | None
    source_pages: list[int]


class SummaryMetric(BaseModel):
    name: str
    value: str


class CreditReportExtraction(BaseModel):
    bureau: str | None = Field(description="equifax, experian or transunion")
    report_date: str | None = Field(
        description="The date this disclosure covers, if the document labels one distinctly from its "
                    "creation date. Leave null if the only date is the creation date."
    )
    document_created_date: str | None = Field(
        description="When this document was produced — a 'Date Created', 'Prepared on' or equivalent. "
                    "This is what makes the report recent."
    )
    consumer_on_file_since: str | None = Field(
        description="How long the bureau has had a file on the consumer ('on file since', 'file established'). "
                    "Historical bureau metadata — NOT the date of this report."
    )
    score_type: str | None = Field(description="e.g. 'FICO Score 8', exactly as labelled")
    score: int | None
    summary_metrics: list[SummaryMetric] = Field(description="Only metrics explicitly printed in the report")
    accounts: list[ExtractedTradeline]
    inquiries: list[ExtractedInquiry]
    public_records: list[PublicRecord]
    unreadable_pages: list[int] = Field(description="Pages that could not be read reliably")
    warnings: list[str] = Field(description="Anything that made extraction uncertain")


# ── Second pass: an independent audit of pass 1 against the same PDF ────────

class AuditFinding(BaseModel):
    kind: str = Field(
        description="One of: correction, missing_account, duplicate_account, unsupported_field, ambiguous"
    )
    account_name: str | None = Field(description="Which account this concerns, by its reported name")
    field: str | None
    extracted_value: str | None = Field(description="What pass 1 said")
    correct_value: str | None = Field(description="What the document actually shows, or null if unsupported")
    page: int | None
    explanation: str


class AuditReport(BaseModel):
    account_count_in_document: int | None = Field(
        description="How many tradelines the document actually contains, counted from the PDF"
    )
    account_count_matches: bool
    identities_correct: bool = Field(
        description="True only if every account is named after the reporting furnisher/collector and any "
                    "original creditor is recorded separately and correctly"
    )
    inquiries_correct: bool
    verified: bool = Field(description="True only if pass 1 is accurate enough to rely on as-is")
    findings: list[AuditFinding]
    confidence: float = Field(description="0.0 to 1.0")
