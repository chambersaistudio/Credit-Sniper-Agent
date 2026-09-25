"""durable background extraction, and account lifecycle vs payment standing

Extraction no longer runs inside the upload request: an Experian PDF can
outlive a browser or proxy connection, and a dropped connection must not lose
a pass we already paid for. Reports now carry their processing stage, timing,
attempt count, an operator-only error class, a SHA-256 of the original for
idempotency, and a checkpoint holding each completed expensive pass.

Separately, credit_accounts gain account_lifecycle (open/closed) and
payment_performance (as_agreed/late_30/charged_off/...). Conflating the two
made "Pays account as agreed" and "Paid or paying as agreed" look like an
open-vs-closed contradiction on an account both bureaus reported as closed.

Existing rows take their extraction_status as their terminal stage, so the
worker never picks up a report that was already processed synchronously.

Revision ID: 0007
Revises: 0006
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_REPORT_COLUMNS = (
    ("processing_started_at", sa.DateTime(timezone=True)),
    ("processing_finished_at", sa.DateTime(timezone=True)),
    ("last_processing_error_class", sa.String()),
    ("document_sha256", sa.String()),
    ("extraction_checkpoint", sa.JSON()),
)


def upgrade() -> None:
    for name, type_ in _REPORT_COLUMNS:
        op.add_column("credit_reports", sa.Column(name, type_, nullable=True))
    # Existing reports were processed synchronously and are already finished.
    # Terminal stages ARE the ExtractionStatus values, so the stage and the
    # quality state can never disagree; backfilling from extraction_status
    # keeps every historical row out of the worker's queue.
    op.add_column("credit_reports", sa.Column(
        "processing_stage", sa.String(), nullable=False, server_default="queued"))
    op.execute("UPDATE credit_reports SET processing_stage = extraction_status")
    op.add_column("credit_reports", sa.Column(
        "attempt_count", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_credit_reports_processing_stage", "credit_reports", ["processing_stage"])
    op.create_index("ix_credit_reports_document_sha256", "credit_reports", ["document_sha256"])

    op.add_column("credit_accounts", sa.Column("account_lifecycle", sa.String(), nullable=True))
    op.add_column("credit_accounts", sa.Column("payment_performance", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("credit_accounts", "payment_performance")
    op.drop_column("credit_accounts", "account_lifecycle")
    op.drop_index("ix_credit_reports_document_sha256", table_name="credit_reports")
    op.drop_index("ix_credit_reports_processing_stage", table_name="credit_reports")
    op.drop_column("credit_reports", "attempt_count")
    op.drop_column("credit_reports", "processing_stage")
    for name, _ in _REPORT_COLUMNS:
        op.drop_column("credit_reports", name)
