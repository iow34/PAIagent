"""Explicit managed memory, source provenance and result feedback."""

from alembic import op

revision = "e1b7c5d9f263"
down_revision = "d0a6b4c8e152"
branch_labels = None
depends_on = None

TABLE_DDL = [
    """CREATE TABLE memory_settings (
    id UUID NOT NULL, 
    version INTEGER NOT NULL, 
    revision UUID NOT NULL, 
    enabled BOOLEAN NOT NULL, 
    config JSONB NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    tenant_id UUID NOT NULL, 
    user_id VARCHAR(255) NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT uq_memory_settings_owner UNIQUE (tenant_id, user_id), 
    FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE memory_messages (
    id UUID NOT NULL, 
    external_id VARCHAR(100) NOT NULL, 
    version INTEGER NOT NULL, 
    content TEXT, 
    fingerprint VARCHAR(64), 
    erased BOOLEAN NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    source_id UUID NOT NULL, 
    collection_id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    user_id VARCHAR(255) NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(tenant_id, source_id, collection_id) REFERENCES integration_collections (tenant_id, source_id, id) ON DELETE RESTRICT, 
    CONSTRAINT uq_memory_message_identity UNIQUE (tenant_id, user_id, collection_id, external_id), 
    FOREIGN KEY(source_id) REFERENCES integration_sources (id) ON DELETE RESTRICT, 
    FOREIGN KEY(collection_id) REFERENCES integration_collections (id) ON DELETE RESTRICT, 
    FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE memory_items (
    id UUID NOT NULL, 
    identity VARCHAR(64) NOT NULL, 
    key VARCHAR(64) NOT NULL, 
    kind VARCHAR(16) NOT NULL, 
    state VARCHAR(16) NOT NULL, 
    version INTEGER NOT NULL, 
    origin VARCHAR(16) NOT NULL, 
    value JSONB, 
    provenance JSONB NOT NULL, 
    period JSONB, 
    delivery_id UUID, 
    message_id UUID, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    source_id UUID NOT NULL, 
    collection_id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    user_id VARCHAR(255) NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(tenant_id, source_id, collection_id) REFERENCES integration_collections (tenant_id, source_id, id) ON DELETE RESTRICT, 
    CONSTRAINT uq_memory_item_identity UNIQUE (tenant_id, user_id, identity), 
    CONSTRAINT ck_memory_kind CHECK (kind IN ('preference','observation')), 
    CONSTRAINT ck_memory_state CHECK (state IN ('candidate','confirmed','superseded','invalidated','deleted','rejected')), 
    FOREIGN KEY(delivery_id) REFERENCES data_deliveries (id) ON DELETE CASCADE, 
    FOREIGN KEY(message_id) REFERENCES memory_messages (id) ON DELETE CASCADE, 
    FOREIGN KEY(source_id) REFERENCES integration_sources (id) ON DELETE RESTRICT, 
    FOREIGN KEY(collection_id) REFERENCES integration_collections (id) ON DELETE RESTRICT, 
    FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE RESTRICT
)""",
    """CREATE INDEX ix_memory_items_owner ON memory_items (tenant_id, user_id, collection_id, id)""",
    """CREATE UNIQUE INDEX uq_memory_confirmed_preference ON memory_items (tenant_id, user_id, collection_id, key) WHERE state = 'confirmed' AND kind = 'preference'""",
    """CREATE TABLE memory_blocks (
    id UUID NOT NULL, 
    key VARCHAR(64) NOT NULL, 
    source_id UUID NOT NULL, 
    collection_id UUID NOT NULL, 
    tenant_id UUID NOT NULL, 
    user_id VARCHAR(255) NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(tenant_id, source_id, collection_id) REFERENCES integration_collections (tenant_id, source_id, id) ON DELETE RESTRICT, 
    CONSTRAINT uq_memory_block UNIQUE (tenant_id, user_id, collection_id, key), 
    FOREIGN KEY(source_id) REFERENCES integration_sources (id) ON DELETE RESTRICT, 
    FOREIGN KEY(collection_id) REFERENCES integration_collections (id) ON DELETE RESTRICT, 
    FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE result_feedback (
    id UUID NOT NULL, 
    result_id UUID NOT NULL, 
    profile_id UUID NOT NULL, 
    request_id VARCHAR(100) NOT NULL, 
    fingerprint VARCHAR(64) NOT NULL, 
    revision INTEGER NOT NULL, 
    rating VARCHAR(16), 
    decision VARCHAR(16), 
    claim_index INTEGER, 
    proposal_fingerprints JSONB NOT NULL, 
    comment VARCHAR(1000) NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    tenant_id UUID NOT NULL, 
    user_id VARCHAR(255) NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT uq_feedback_request UNIQUE (tenant_id, user_id, request_id), 
    FOREIGN KEY(result_id) REFERENCES analysis_results (id) ON DELETE CASCADE, 
    FOREIGN KEY(profile_id) REFERENCES analysis_profiles (id) ON DELETE CASCADE, 
    FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE RESTRICT
)""",
    """CREATE INDEX ix_feedback_owner_profile ON result_feedback (tenant_id, user_id, profile_id, revision)""",
    """CREATE TABLE personalization_uses (
    result_id UUID NOT NULL, 
    memory_id UUID NOT NULL, 
    memory_version INTEGER NOT NULL, 
    PRIMARY KEY (result_id, memory_id), 
    FOREIGN KEY(result_id) REFERENCES analysis_results (id) ON DELETE CASCADE, 
    FOREIGN KEY(memory_id) REFERENCES memory_items (id) ON DELETE CASCADE
)""",
]


def upgrade():
    for statement in TABLE_DDL:
        op.execute(statement)


def downgrade():
    op.drop_table("personalization_uses")
    op.drop_table("result_feedback")
    op.drop_table("memory_blocks")
    op.drop_table("memory_items")
    op.drop_table("memory_messages")
    op.drop_table("memory_settings")
