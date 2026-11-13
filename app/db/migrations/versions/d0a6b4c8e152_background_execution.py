"""Durable analysis jobs, trigger cursors and tenant reservations."""

import sqlalchemy as sa
from alembic import op

revision = "d0a6b4c8e152"
down_revision = "c9f5a3b7d041"
branch_labels = None
depends_on = None

# Frozen PostgreSQL DDL: no imports from application models at migration time.
TABLE_DDL = [
    """CREATE TABLE analysis_triggers (
    profile_id UUID NOT NULL, 
    profile_version INTEGER NOT NULL, 
    key_id UUID, 
    grant_scopes JSONB NOT NULL, 
    grant_sources JSONB, 
    collection_ids JSONB NOT NULL, 
    config JSONB NOT NULL, 
    revision UUID NOT NULL, 
    enabled BOOLEAN NOT NULL, 
    last_signature VARCHAR(64) NOT NULL, 
    next_due_at TIMESTAMP WITH TIME ZONE, 
    checked_at TIMESTAMP WITH TIME ZONE, 
    error_code VARCHAR(64), 
    tenant_id UUID NOT NULL, 
    user_id VARCHAR(255) NOT NULL, 
    PRIMARY KEY (profile_id), 
    FOREIGN KEY(tenant_id, user_id, profile_id) REFERENCES analysis_profiles (tenant_id, user_id, id) ON DELETE CASCADE, 
    FOREIGN KEY(profile_id, profile_version) REFERENCES analysis_profile_versions (profile_id, version) ON DELETE CASCADE, 
    FOREIGN KEY(key_id) REFERENCES service_api_keys (id) ON DELETE SET NULL, 
    FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE RESTRICT
)""",
    """CREATE INDEX ix_analysis_triggers_poll ON analysis_triggers (enabled, checked_at)""",
    """CREATE TABLE analysis_runs (
    id UUID NOT NULL, 
    profile_id UUID NOT NULL, 
    profile_version INTEGER NOT NULL, 
    key_id UUID, 
    grant_scopes JSONB NOT NULL, 
    grant_sources JSONB, 
    collection_ids JSONB NOT NULL, 
    required_scopes JSONB NOT NULL, 
    trigger_id UUID, 
    trigger_revision UUID, 
    budget JSONB NOT NULL, 
    input JSONB NOT NULL, 
    fingerprint VARCHAR(64) NOT NULL, 
    idempotency_key VARCHAR(100), 
    reason VARCHAR(16) NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    attempts INTEGER NOT NULL, 
    max_attempts INTEGER NOT NULL, 
    retry_seconds INTEGER NOT NULL, 
    ephemeral BOOLEAN NOT NULL, 
    lease_token UUID, 
    lease_expires_at TIMESTAMP WITH TIME ZONE, 
    input_boundary VARCHAR(64), 
    data_as_of TIMESTAMP WITH TIME ZONE, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    available_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    started_at TIMESTAMP WITH TIME ZONE, 
    finished_at TIMESTAMP WITH TIME ZONE, 
    error_code VARCHAR(64), 
    tenant_id UUID NOT NULL, 
    user_id VARCHAR(255) NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT ck_analysis_run_lease CHECK ((status = 'running') = (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)), 
    FOREIGN KEY(tenant_id, user_id, profile_id) REFERENCES analysis_profiles (tenant_id, user_id, id) ON DELETE CASCADE, 
    FOREIGN KEY(profile_id, profile_version) REFERENCES analysis_profile_versions (profile_id, version) ON DELETE CASCADE, 
    CONSTRAINT uq_analysis_run_idempotency UNIQUE (tenant_id, user_id, idempotency_key), 
    CONSTRAINT ck_analysis_run_status CHECK (status IN ('pending','running','succeeded','failed','cancelled')), 
    CONSTRAINT ck_analysis_run_attempts CHECK (attempts >= 0 AND max_attempts BETWEEN 1 AND 3), 
    FOREIGN KEY(key_id) REFERENCES service_api_keys (id) ON DELETE SET NULL, 
    FOREIGN KEY(trigger_id) REFERENCES analysis_triggers (profile_id) ON DELETE SET NULL, 
    FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE RESTRICT
)""",
    """CREATE INDEX ix_analysis_runs_due ON analysis_runs (status, available_at, tenant_id)""",
    """CREATE INDEX ix_analysis_runs_owner ON analysis_runs (tenant_id, user_id, id)""",
    """CREATE UNIQUE INDEX uq_analysis_trigger_pending ON analysis_runs (trigger_id) WHERE status = 'pending' AND trigger_id IS NOT NULL""",
    """CREATE TABLE analysis_tenant_usage (
    tenant_id UUID NOT NULL, 
    day DATE NOT NULL, 
    attempts INTEGER NOT NULL, 
    calls INTEGER NOT NULL, 
    input_tokens INTEGER NOT NULL, 
    output_tokens INTEGER NOT NULL, 
    last_claimed_at TIMESTAMP WITH TIME ZONE, 
    PRIMARY KEY (tenant_id, day), 
    FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
)""",
]


def upgrade():
    for statement in TABLE_DDL:
        op.execute(statement)
    op.add_column("analysis_results", sa.Column("run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_analysis_results_run",
        "analysis_results",
        "analysis_runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_analysis_results_run", "analysis_results", ["run_id"]
    )


def downgrade():
    op.drop_constraint(
        "fk_analysis_results_run", "analysis_results", type_="foreignkey"
    )
    op.drop_constraint("uq_analysis_results_run", "analysis_results", type_="unique")
    op.drop_column("analysis_results", "run_id")
    op.drop_table("analysis_tenant_usage")
    op.drop_table("analysis_runs")
    op.drop_table("analysis_triggers")
