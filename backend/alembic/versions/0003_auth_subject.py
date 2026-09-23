"""user auth_subject

Adds the auth provider's stable subject id to users. The signed-in user is
matched to their row by this column; it is unique and indexed. NULL for the
legacy local dev user (auth disabled).

Revision ID: 0003
Revises: 0002
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("auth_subject", sa.String(), nullable=True))
    op.create_index("ix_users_auth_subject", "users", ["auth_subject"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_auth_subject", table_name="users")
    op.drop_column("users", "auth_subject")
