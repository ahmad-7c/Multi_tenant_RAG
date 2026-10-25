"""create organizations and users

Revision ID: 0001
Revises:
Create Date: 2026-10-09

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Created explicitly, then referenced below with create_type=False.
    # Without that flag SQLAlchemy emits a second (un-guarded) CREATE TYPE
    # as part of create_table and the migration dies with
    # "type user_role already exists" on a completely fresh database.
    sa.Enum("admin", "member", name="user_role").create(op.get_bind(), checkfirst=True)
    user_role = postgresql.ENUM("admin", "member", name="user_role", create_type=False)

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("role", user_role, nullable=False, server_default="member"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        # Same email can exist across different orgs, so uniqueness is
        # scoped to (org_id, email) rather than email alone.
        sa.UniqueConstraint("org_id", "email", name="uq_users_org_id_email"),
    )
    op.create_index("ix_users_org_id", "users", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_users_org_id", table_name="users")
    op.drop_table("users")
    sa.Enum(name="user_role").drop(op.get_bind(), checkfirst=True)
    op.drop_table("organizations")
