"""Add conversation sessions and messages.

Revision ID: d9e3f6a2b845
Revises: c4d8e2f9a631
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d9e3f6a2b845"
down_revision = "c4d8e2f9a631"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_sessions",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
        ),
    )

    op.create_index(
        "ix_conversation_sessions_owner",
        "conversation_sessions",
        ["tenant_id", "user_id"],
        unique=False,
    )

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("answer_status", sa.String(length=32), nullable=True),
        sa.Column(
            "sources",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["conversation_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "session_id",
            "position",
            name="uq_conversation_messages_session_position",
        ),
        sa.UniqueConstraint(
            "session_id",
            "request_id",
            "role",
            name="uq_conversation_messages_session_request_role",
        ),
        sa.CheckConstraint(
            "position >= 0",
            name="ck_conversation_messages_position",
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_conversation_messages_role",
        ),
        sa.CheckConstraint(
            "(role = 'user' AND answer_status IS NULL) OR "
            "(role = 'assistant' AND answer_status IS NOT NULL "
            "AND answer_status IN ('answered', 'insufficient_information'))",
            name="ck_conversation_messages_answer_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(sources) = 'array'",
            name="ck_conversation_messages_sources_array",
        ),
    )


def downgrade() -> None:
    op.drop_table("conversation_messages")

    op.drop_index(
        "ix_conversation_sessions_owner",
        table_name="conversation_sessions",
    )

    op.drop_table("conversation_sessions")
