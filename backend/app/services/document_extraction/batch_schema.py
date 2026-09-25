"""
Stage 2's output: detailed tradelines, and nothing else.

Reuses `ExtractedTradeline` unchanged — the full account record, exactly as
single-pass extraction produced it, so everything downstream (mapping,
account semantics, matching, the audit) works on batched output without
knowing it was batched.

What is deliberately ABSENT is the report level. Bureau, document date, score,
score type and the tradeline count were all banked by Stage 1; asking for them
again would pay four times for one answer and invite four different answers to
the same question.
"""
from pydantic import BaseModel, Field

from app.services.document_extraction.schema import ExtractedTradeline


class TradelineBatch(BaseModel):
    """The detailed reading of one batch of tradelines."""

    accounts: list[ExtractedTradeline] = Field(
        description="One entry per tradeline you were asked for, in the same order, "
                    "read in full detail from the attached pages"
    )
    missing_tradelines: list[str] = Field(
        description="Names of any tradeline you were asked for that does not appear on the "
                    "attached pages. Say so here rather than inventing an entry for it."
    )
    unreadable_pages: list[int] = Field(
        description="Pages of THIS attached document that could not be read reliably, "
                    "numbered as they appear here starting at 1"
    )
