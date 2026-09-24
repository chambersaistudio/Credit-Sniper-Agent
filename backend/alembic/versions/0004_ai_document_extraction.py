"""AI-native document extraction

Adds the explicit extraction-quality state and audit trail to reports, and
the fields the canonical document schema can now capture on a tradeline —
most importantly the furnisher/original-creditor/sold-to distinction, the
month-by-month payment grid, and per-field source provenance.

Existing rows predate AI document extraction, so they are marked
"extraction_incomplete": they were never verified against the original PDF
and must not feed dispute analysis until re-ingested.

Revision ID: 0004
Revises: 0003
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_REPORT_COLUMNS = (
    ("score_type", sa.String()),
    ("extraction_audit", sa.JSON()),
    ("public_records", sa.JSON()),
)
_ACCOUNT_COLUMNS = (
    ("original_creditor", sa.String()),
    ("sold_to", sa.String()),
    ("account_status_raw", sa.String()),
    ("date_status_updated", sa.String()),
    ("terms", sa.String()),
    ("responsibility", sa.String()),
    ("consumer_dispute", sa.String()),
    ("contact", sa.JSON()),
    ("source_pages", sa.JSON()),
    ("field_evidence", sa.JSON()),
)


def upgrade() -> None:
    for name, type_ in _REPORT_COLUMNS:
        op.add_column("credit_reports", sa.Column(name, type_, nullable=True))
    op.add_column(
        "credit_reports",
        sa.Column("extraction_status", sa.String(), nullable=False,
                  server_default="extraction_incomplete"),
    )
    for name, type_ in _ACCOUNT_COLUMNS:
        op.add_column("credit_accounts", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in _ACCOUNT_COLUMNS:
        op.drop_column("credit_accounts", name)
    op.drop_column("credit_reports", "extraction_status")
    for name, _ in _REPORT_COLUMNS:
        op.drop_column("credit_reports", name)
