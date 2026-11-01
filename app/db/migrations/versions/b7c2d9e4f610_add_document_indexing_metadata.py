"""Add document indexing metadata and invalidate legacy ready statuses.

Revision ID: b7c2d9e4f610
Revises: a8ef3f18278b
"""

import sqlalchemy as sa
from alembic import op

revision = "b7c2d9e4f610"
down_revision = "a8ef3f18278b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents", sa.Column("embedding_model", sa.String(100), nullable=True)
    )
    op.add_column(
        "documents", sa.Column("embedding_dimensions", sa.Integer(), nullable=True)
    )
    op.add_column(
        "documents", sa.Column("embedding_tokens", sa.Integer(), nullable=True)
    )
    op.add_column(
        "documents", sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "documents", sa.Column("indexing_error_code", sa.String(50), nullable=True)
    )

    # Old ready meant that texts were stored, not that embeddings were generated.
    # Preserve the documents and chunks, but require explicit reindexing.
    op.execute(
        sa.text("UPDATE documents SET status = 'pending' WHERE status = 'ready'")
    )


def downgrade() -> None:
    # Do not mark pending documents ready without proof of successful indexing.
    op.drop_column("documents", "indexing_error_code")
    op.drop_column("documents", "indexed_at")
    op.drop_column("documents", "embedding_tokens")
    op.drop_column("documents", "embedding_dimensions")
    op.drop_column("documents", "embedding_model")
