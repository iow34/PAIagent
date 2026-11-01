"""Add indexing attempt identity and expiry.

Revision ID: c4d8e2f9a631
Revises: b7c2d9e4f610
"""

import sqlalchemy as sa
from alembic import op

revision = "c4d8e2f9a631"
down_revision = "b7c2d9e4f610"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("indexing_attempt_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column("indexing_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "indexing_expires_at")
    op.drop_column("documents", "indexing_attempt_id")
