"""Integration registry and explicit API-key grants.

Revision ID: f3a6c9d2e710
Revises: e2f4b8c6a901
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "f3a6c9d2e710"
down_revision = "e2f4b8c6a901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "service_api_keys",
        sa.Column(
            "scopes",
            JSONB(),
            nullable=False,
            server_default=sa.text('\'["legacy:*", "catalog:read"]\'::jsonb'),
        ),
    )
    op.add_column("service_api_keys", sa.Column("source_ids", JSONB(), nullable=True))
    op.create_table(
        "integration_sources",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(1000), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_integration_source_name"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_integration_source_tenant_id"),
    )
    op.create_table(
        "integration_collections",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(1000), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_id"],
            ["integration_sources.tenant_id", "integration_sources.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("source_id", "name", name="uq_integration_collection_name"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_integration_collection_tenant_id"
        ),
    )
    op.create_table(
        "integration_schemas",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.UUID(),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("collection_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("json_schema", JSONB(), nullable=False),
        sa.Column("field_mappings", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "collection_id"],
            ["integration_collections.tenant_id", "integration_collections.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "collection_id", "kind", "version", name="uq_integration_schema_version"
        ),
        sa.CheckConstraint(
            "kind IN ('record', 'event')", name="ck_integration_schema_kind"
        ),
        sa.CheckConstraint("version >= 1", name="ck_integration_schema_version"),
        sa.CheckConstraint(
            "jsonb_typeof(json_schema) = 'object'", name="ck_integration_schema_object"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(field_mappings) = 'array'",
            name="ck_integration_schema_mappings",
        ),
    )


def downgrade() -> None:
    op.drop_table("integration_schemas")
    op.drop_table("integration_collections")
    op.drop_table("integration_sources")
    op.drop_column("service_api_keys", "source_ids")
    op.drop_column("service_api_keys", "scopes")
