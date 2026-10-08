"""
Stage 1 of scaled extraction: the report index.

Deliberately the cheapest possible reading of the document. It answers only
"what is in here and where", so that stage 2 can ask for one small batch of
tradelines at a time instead of demanding the entire report in a single
structured output.

What made the single-pass extraction fail is exactly what is absent here:
no balances, no dates per account, no month-by-month payment grid, no field
evidence. A fully detailed tradeline costs ~2,100 output tokens; an index
entry costs roughly eighty.

The self-declared `tradeline_count` is not redundant with `len(tradelines)`.
A truncated or lazy listing is the failure mode that matters most at this
stage — an index that silently stops at nine accounts would quietly lose six
tradelines from every later batch — so the model states the total separately
and the two are compared.
"""
from pydantic import BaseModel, Field


class IndexedTradeline(BaseModel):
    """One tradeline's identity and location. Nothing about its contents."""

    creditor_name: str = Field(
        description="The company actually reporting this tradeline — the furnisher, or the "
                    "collection agency named as the account. NOT the original creditor."
    )
    original_creditor: str | None = Field(
        description="Original creditor when the report names one (typical for collections). "
                    "Recorded here only to tell two entries apart; null otherwise."
    )
    account_number: str | None = Field(
        description="Masked account number exactly as printed, or null if none is shown"
    )
    account_type: str | None = Field(
        description="The account type as labelled, e.g. 'Credit card'. Null if not printed."
    )
    source_pages: list[int] = Field(
        description="Every 1-based page this tradeline appears on. Required — a tradeline that "
                    "cannot be located cannot be read in detail later."
    )
    heading_excerpt: str | None = Field(
        description="Short verbatim excerpt of the account heading, proving this entry's identity"
    )


class IndexedContact(BaseModel):
    """Low-volume contact details printed with an inquiry."""

    name: str | None = None
    address: str | None = None
    phone: str | None = None


class IndexedInquiry(BaseModel):
    """One inquiry, kept lightweight so Stage 1 remains cheap."""

    creditor_name: str
    inquiry_date: str | None = None
    inquiry_type: str | None = Field(
        description="hard or soft only when the report itself establishes that classification"
    )
    inquiry_category: str | None = Field(
        description="Why the inquiry occurred from the section heading, when the report states it"
    )
    business_type: str | None = Field(
        description="The company industry/business type as printed; not the hard/soft classification"
    )
    contact: IndexedContact | None = Field(
        default=None,
        description="Inquiry contact name/address/phone exactly as printed, when present"
    )
    source_pages: list[int] = Field(default_factory=list)


class IndexedPublicRecord(BaseModel):
    record_type: str | None = None
    status: str | None = None
    filed_date: str | None = None
    amount: str | None = None
    reference: str | None = None
    source_pages: list[int] = Field(default_factory=list)


class IndexedSummaryMetric(BaseModel):
    name: str
    value: str

class ReportIndex(BaseModel):
    """Report-level facts plus a located list of every tradeline."""

    bureau: str | None = Field(description="equifax, experian or transunion")
    document_created_date: str | None = Field(
        description="When this document was produced — a 'Date Created', 'Prepared on' or "
                    "equivalent. This is what makes the report recent."
    )
    report_date: str | None = Field(
        description="The date this disclosure covers, if the document labels one distinctly "
                    "from its creation date. Null if the only date is the creation date."
    )
    score: int | None
    score_type: str | None = Field(description="e.g. 'FICO Score 8', exactly as labelled")
    consumer_on_file_since: str | None = Field(
        default=None,
        description="How long the bureau says the consumer file has existed, if explicitly printed"
    )
    summary_metrics: list[IndexedSummaryMetric] = Field(
        default_factory=list,
        description="Small report-level summary values explicitly printed outside tradeline detail"
    )
    inquiries: list[IndexedInquiry] = Field(
        default_factory=list,
        description="Every inquiry listed by the report, with its page location"
    )
    public_records: list[IndexedPublicRecord] = Field(
        default_factory=list,
        description="Every public record explicitly listed; empty when the report says there are none"
    )
    tradeline_count: int | None = Field(
        description="How many tradelines the report contains in total, counted from the "
                    "document itself. State this independently of the list below."
    )
    tradelines: list[IndexedTradeline] = Field(
        description="Every tradeline in the report, including closed and collection accounts, "
                    "each listed exactly once"
    )
    total_pages: int | None = Field(description="How many pages the document has")
    unreadable_pages: list[int] = Field(description="Pages that could not be read reliably")
