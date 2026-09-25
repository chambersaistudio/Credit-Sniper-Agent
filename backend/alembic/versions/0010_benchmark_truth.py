"""server-side benchmark truth

Benchmark truth holds real account values, so it is stored here rather than
pasted into a UI, a chat or a repository. Entered or corrected once, then
selected by reference.

`verified` exists because truth drafted from a model's own extraction would
make a benchmark measure agreement with that model rather than correctness. A
draft starts unverified and the benchmark refuses it.

Revision ID: 0010
Revises: 0009
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "benchmark_truth",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("report_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("credit_reports.id", ondelete="CASCADE"), nullable=False),
        sa.Column("batch_id", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False, server_default="current"),
        sa.Column("accounts", postgresql.JSONB(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source", sa.String(), nullable=False, server_default="operator"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_benchmark_truth_report_id", "benchmark_truth", ["report_id"])
    op.create_index("ix_benchmark_truth_fingerprint", "benchmark_truth", ["fingerprint"])
    op.create_unique_constraint(
        "uq_benchmark_truth_report_batch_label",
        "benchmark_truth", ["report_id", "batch_id", "label"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_benchmark_truth_report_batch_label", "benchmark_truth", type_="unique")
    op.drop_index("ix_benchmark_truth_fingerprint", table_name="benchmark_truth")
    op.drop_index("ix_benchmark_truth_report_id", table_name="benchmark_truth")
    op.drop_table("benchmark_truth")
