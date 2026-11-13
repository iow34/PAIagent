"""Immutable analysis profiles and successful collected-data results."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c9f5a3b7d041"
down_revision = "b8e4f2a6c930"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "analysis_profiles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_analysis_profile_owner_id",
        "analysis_profiles",
        ["tenant_id", "user_id", "id"],
    )
    op.create_index(
        "ix_analysis_profiles_owner",
        "analysis_profiles",
        ["tenant_id", "user_id", "id"],
    )
    op.create_table(
        "analysis_profile_versions",
        sa.Column(
            "profile_id",
            sa.Uuid(),
            sa.ForeignKey("analysis_profiles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name="ck_analysis_profile_version"),
    )
    op.create_table(
        "analysis_results",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("collection_ids", postgresql.JSONB(), nullable=False),
        sa.Column("envelope", postgresql.JSONB(), nullable=False),
        sa.Column("snapshot", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["profile_id", "profile_version"],
            [
                "analysis_profile_versions.profile_id",
                "analysis_profile_versions.version",
            ],
            ondelete="CASCADE",
        ),
    )
    op.create_foreign_key(
        "fk_analysis_result_owner",
        "analysis_results",
        "analysis_profiles",
        ["tenant_id", "user_id", "profile_id"],
        ["tenant_id", "user_id", "id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_analysis_results_owner",
        "analysis_results",
        ["tenant_id", "user_id", "profile_id", "created_at"],
    )


def downgrade():
    op.drop_table("analysis_results")
    op.drop_table("analysis_profile_versions")
    op.drop_table("analysis_profiles")
