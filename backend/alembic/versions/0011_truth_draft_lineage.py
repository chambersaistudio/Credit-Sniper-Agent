"""which model drafted a truth set

A truth set can now be drafted by a paid model read of the batch, for a report
with nothing banked to draft from. A human corrects and verifies it before any
benchmark will use it — but correcting a prefilled draft anchors on it: an
error the reviewer does not notice survives, and it is the drafting model's
error. So the lineage is kept, through corrections, and a benchmark of the same
model against truth it drafted says so.

Revision ID: 0011
Revises: 0010
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("benchmark_truth", sa.Column("drafted_by_model", sa.String(), nullable=True))
    op.add_column("benchmark_truth", sa.Column("drafted_by_config", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("benchmark_truth", "drafted_by_config")
    op.drop_column("benchmark_truth", "drafted_by_model")
