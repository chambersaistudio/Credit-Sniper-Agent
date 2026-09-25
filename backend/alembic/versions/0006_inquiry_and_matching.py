"""inquiry category, file-since date, ambiguous match review

From the first live TransUnion disclosure:

  * It separates "Promotional Inquiries" and "Account Review Inquiries" and
    states they are consumer-visible and non-scoring. inquiry_category records
    which section an inquiry came from so those never count as hard inquiries.
  * It prints a "Date Created" alongside a much older "on file since" date.
    on_file_since keeps that historical metadata away from the report date.
  * Cross-bureau matching now has three outcomes; match_review records the
    near-miss when a canonical account was created despite resembling
    another, so ambiguity is reviewable instead of silently merged.

Revision ID: 0006
Revises: 0005
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("credit_inquiries", sa.Column("inquiry_category", sa.String(), nullable=True))
    op.add_column("credit_reports", sa.Column("on_file_since", sa.String(), nullable=True))
    op.add_column("canonical_accounts", sa.Column("match_review", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("canonical_accounts", "match_review")
    op.drop_column("credit_reports", "on_file_since")
    op.drop_column("credit_inquiries", "inquiry_category")
