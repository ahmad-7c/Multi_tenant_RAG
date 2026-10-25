"""create conversations and messages

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-25

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "org_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False
        ),
        # Conversations are private to the user who started them: a
        # question reveals what someone was looking into.
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_conversations_org_id", "conversations", ["org_id"])
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    # Listing is always scoped to both, so the composite index serves the
    # only read pattern there is.
    op.create_index("ix_conversations_org_user", "conversations", ["org_id", "user_id"])

    # Created once, then referenced with create_type=False: create_table
    # would otherwise emit a second, unguarded CREATE TYPE and fail on a
    # fresh database (see 0001).
    sa.Enum("user", "assistant", name="message_role").create(
        op.get_bind(), checkfirst=True
    )
    message_role = postgresql.ENUM(
        "user", "assistant", name="message_role", create_type=False
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Denormalized from conversations.org_id for the same reason
        # document_chunks carries it: the tenant filter must not depend on
        # a join.
        sa.Column(
            "org_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False
        ),
        sa.Column("role", message_role, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        # An array rather than a join table: read back whole with the
        # message, never queried across messages.
        sa.Column(
            "cited_chunk_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_index("ix_messages_org_id", "messages", ["org_id"])
    # Carries the sort column: reading a conversation always means "this
    # conversation, in order".
    op.create_index(
        "ix_messages_conversation_created", "messages", ["conversation_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_messages_conversation_created", table_name="messages")
    op.drop_index("ix_messages_org_id", table_name="messages")
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_table("messages")
    sa.Enum(name="message_role").drop(op.get_bind(), checkfirst=True)
    op.drop_index("ix_conversations_org_user", table_name="conversations")
    op.drop_index("ix_conversations_user_id", table_name="conversations")
    op.drop_index("ix_conversations_org_id", table_name="conversations")
    op.drop_table("conversations")
