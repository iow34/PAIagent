"""Fix tenant foreign key and document index

Revision ID: a8ef3f18278b
Revises: 8b72c4d91e06
Create Date: 2026-10-22 22:48:33.564035

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8ef3f18278b"
down_revision: Union[str, Sequence[str], None] = "8b72c4d91e06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_foreign_key(
        "fk_service_api_keys_tenant_id_tenants",
        "service_api_keys",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.execute("ALTER INDEX ix_documents_tenant_id RENAME TO ix_documents_tenant_user")


def downgrade() -> None:
    op.execute("ALTER INDEX ix_documents_tenant_user RENAME TO ix_documents_tenant_id")

    op.drop_constraint(
        "fk_service_api_keys_tenant_id_tenants",
        "service_api_keys",
        type_="foreignkey",
    )
