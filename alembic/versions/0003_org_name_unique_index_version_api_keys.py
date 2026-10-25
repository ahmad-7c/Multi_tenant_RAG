"""phase 1 fixes: unique org name, index_version, api_keys

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-25

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Login resolves an account by (org name, email). Without this
    # constraint two orgs could share a name and the lookup would be
    # ambiguous -- whichever row Postgres returned first would silently
    # decide which tenant you authenticated into.
    op.create_unique_constraint("uq_organizations_name", "organizations", ["name"])

    # Part of the answer-cache key. Bumping it invalidates every cached
    # answer for one org atomically, instead of scanning Redis for that
    # org's keys. Phase 2 increments it on ingest-READY and on delete.
    op.add_column(
        "organizations",
        sa.Column("index_version", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        # Hash only -- the raw key is shown once and never stored, same
        # rule as a password.
        sa.Column("hashed_key", sa.String(255), nullable=False),
        sa.Column("key_prefix", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("key_prefix", name="uq_api_keys_key_prefix"),
    )
    op.create_index("ix_api_keys_org_id", "api_keys", ["org_id"])
    # Unique index on the prefix makes authenticating a presented key an
    # indexed point read; without it every request would have to bcrypt-
    # verify the candidate against every key row in the table.
    op.create_index("ix_api_keys_key_prefix", "api_keys", ["key_prefix"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_key_prefix", table_name="api_keys")
    op.drop_index("ix_api_keys_org_id", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_column("organizations", "index_version")
    op.drop_constraint("uq_organizations_name", "organizations", type_="unique")
