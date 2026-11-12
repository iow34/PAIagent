"""Add document knowledge storage.

Revision ID: e2f4b8c6a901
Revises: d9e3f6a2b845
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e2f4b8c6a901"
down_revision = "d9e3f6a2b845"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_knowledge",
        sa.Column(
            "document_id",
            sa.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "summary",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "summary_chunk_ids",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "topics",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "facts",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "sources",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "coverage",
            sa.String(length=16),
            nullable=False,
        ),
        sa.Column(
            "generation_model",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("document_id"),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "coverage IN ('full', 'partial')",
            name="ck_document_knowledge_coverage",
        ),
        sa.CheckConstraint(
            "length(btrim(summary)) > 0",
            name="ck_document_knowledge_summary_nonempty",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(summary_chunk_ids) = 'array'",
            name="ck_document_knowledge_summary_chunk_ids_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(topics) = 'array'",
            name="ck_document_knowledge_topics_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(facts) = 'array'",
            name="ck_document_knowledge_facts_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(sources) = 'array'",
            name="ck_document_knowledge_sources_array",
        ),
    )


def downgrade() -> None:
    op.drop_table("document_knowledge")
