"""separate conflated report fields

Three concepts were being folded into fields that mean something else, which
made a correct extraction look wrong:

  * "Balance updated" was written into date_last_reported. It is its own
    date; date_last_reported now stays null unless the report labels one.
  * Page/section labels ("Potentially negative", "Exceptional payment
    history") could land in payment_status. They describe the report's
    layout, not the account, and now have their own column.
  * An inquiry's "Business Type" ("Bank Credit Cards") is the company's
    industry, not the hard/soft inquiry_type.

Existing rows keep whatever they had; re-ingesting a report repopulates the
new columns correctly.

Revision ID: 0005
Revises: 0004
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("credit_accounts", sa.Column("balance_updated_date", sa.String(), nullable=True))
    op.add_column("credit_accounts", sa.Column("report_classification", sa.String(), nullable=True))
    op.add_column("credit_inquiries", sa.Column("business_type", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("credit_inquiries", "business_type")
    op.drop_column("credit_accounts", "report_classification")
    op.drop_column("credit_accounts", "balance_updated_date")
