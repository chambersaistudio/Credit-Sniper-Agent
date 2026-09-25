"""atomic claiming for extraction work

The extraction worker selected a pending report with a plain SELECT and then
processed it. Two API instances could select the same row and both pay to read
the same document. A claim timestamp plus an atomic conditional UPDATE makes
the claim indivisible, the same way operator_jobs already does it.

Revision ID: 0009
Revises: 0008
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # When this report was last claimed by a worker. Distinct from
    # processing_started_at, which records when processing FIRST began and is
    # deliberately preserved across retries.
    op.add_column("credit_reports",
                  sa.Column("processing_claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_credit_reports_processing_claimed_at",
                    "credit_reports", ["processing_claimed_at"])


def downgrade() -> None:
    op.drop_index("ix_credit_reports_processing_claimed_at", table_name="credit_reports")
    op.drop_column("credit_reports", "processing_claimed_at")
