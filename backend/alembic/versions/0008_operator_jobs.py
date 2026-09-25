"""durable operator jobs

The operator control plane turns each allowlisted operation into a row so it
survives a restart, can be claimed by exactly one worker, and leaves an audit
record of who asked for what, what it was allowed to spend and what it cost.

The idempotency key is unique per principal, which is what stops a retried
request — a flaky phone connection, an agent re-sending — from buying the same
provider work twice.

Revision ID: 0008
Revises: 0007
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operator_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("report_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column("request_json", postgresql.JSONB(), nullable=True),
        sa.Column("result_json", postgresql.JSONB(), nullable=True),
        sa.Column("safe_error_class", sa.String(), nullable=True),
        sa.Column("safe_error_message", sa.Text(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=True),
        sa.Column("max_model_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model_calls_made", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.String(), nullable=True),
        sa.Column("request_fingerprint", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_operator_jobs_operation", "operator_jobs", ["operation"])
    op.create_index("ix_operator_jobs_status", "operator_jobs", ["status"])
    op.create_index("ix_operator_jobs_report_id", "operator_jobs", ["report_id"])
    op.create_index("ix_operator_jobs_created_at", "operator_jobs", ["created_at"])
    op.create_index("ix_operator_jobs_idempotency_key", "operator_jobs", ["idempotency_key"])
    # Per principal, not global: two operators may each use "b0-luna" without
    # one silently receiving the other's job.
    op.create_unique_constraint(
        "uq_operator_jobs_principal_idempotency",
        "operator_jobs", ["requested_by", "idempotency_key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_operator_jobs_principal_idempotency", "operator_jobs", type_="unique")
    for name in ("idempotency_key", "created_at", "report_id", "status", "operation"):
        op.drop_index(f"ix_operator_jobs_{name}", table_name="operator_jobs")
    op.drop_table("operator_jobs")
