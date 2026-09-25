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
