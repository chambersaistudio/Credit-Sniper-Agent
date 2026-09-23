"""document storage keys

Report PDFs and approved dispute letters move behind the storage layer
(local directory or private R2 bucket), addressed by key instead of a
server file path.

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("credit_reports", "file_path", new_column_name="storage_key")
    op.add_column("cases", sa.Column("approved_package_key", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("cases", "approved_package_key")
    op.alter_column("credit_reports", "storage_key", new_column_name="file_path")
