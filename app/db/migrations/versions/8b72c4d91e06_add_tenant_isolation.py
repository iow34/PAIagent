"""Add tenants, service API keys, and document ownership.

Revision ID: 8b72c4d91e06
Revises: 21cfe15ba2e5
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "8b72c4d91e06"
down_revision: Union[str, Sequence[str], None] = "21cfe15ba2e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEV_TENANT_ID = "00000000-0000-4000-8000-000000000001"


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "service_api_keys",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )

    op.create_index(
        "ix_service_api_keys_tenant_id",
        "service_api_keys",
        ["tenant_id"],
    )

    op.add_column(
        "documents",
        sa.Column("tenant_id", sa.UUID(), nullable=True),
    )

    connection = op.get_bind()

    connection.execute(
        sa.text(
            """
            INSERT INTO tenants (id, name)
            VALUES (CAST(:tenant_id AS uuid), :name)
            """
        ),
        {
            "tenant_id": DEV_TENANT_ID,
            "name": "Development tenant",
        },
    )

    connection.execute(
        sa.text(
            """
            UPDATE documents
            SET tenant_id = CAST(:tenant_id AS uuid)
            WHERE tenant_id IS NULL
            """
        ),
        {
            "tenant_id": DEV_TENANT_ID,
        },
    )

    op.alter_column(
        "documents",
        "tenant_id",
        existing_type=sa.UUID(),
        nullable=False,
    )

    op.create_foreign_key(
        "fk_documents_tenant_id_tenants",
        "documents",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.drop_index(
        "ix_documents_user_id",
        table_name="documents",
    )

    op.create_index(
        "ix_documents_tenant_id",
        "documents",
        ["tenant_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_documents_tenant_id",
        table_name="documents",
    )

    op.create_index(
        "ix_documents_user_id",
        "documents",
        ["user_id"],
    )

    op.drop_constraint(
        "fk_documents_tenant_id_tenants",
        "documents",
        type_="foreignkey",
    )

    op.drop_column("documents", "tenant_id")

    op.drop_index(
        "ix_service_api_keys_tenant_id",
        table_name="service_api_keys",
    )

    op.drop_table("service_api_keys")
    op.drop_table("tenants")