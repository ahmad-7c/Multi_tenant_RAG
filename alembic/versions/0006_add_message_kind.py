"""add messages.kind

Marks what an assistant turn actually is, so the query rewriter can be
shown answers only. A refusal in the rewrite history becomes the most
recent assistant "topic" and leaks into the next rewritten question --
and therefore into the retrieval query and the cache key.

Nullable on purpose. NULL covers both user turns (where kind does not
apply) and every assistant row written before this migration. Treating
those legacy rows as answers is the safe default: the alternative would
silently empty the rewrite history of every existing conversation.

Revision ID: 0006
Revises: 0005
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MESSAGE_KIND = "message_kind"
VALUES = ("answer", "refusal", "error")


def upgrade() -> None:
    # Created explicitly, then referenced with create_type=False below.
    # Passing a fresh sa.Enum into add_column would emit a SECOND
    # CREATE TYPE and fail with DuplicateObject -- the exact bug that
    # made migrations 0001/0002 unrunnable on a fresh database.
    postgresql.ENUM(*VALUES, name=MESSAGE_KIND).create(op.get_bind(), checkfirst=True)

    op.add_column(
        "messages",
        sa.Column(
            "kind",
            postgresql.ENUM(*VALUES, name=MESSAGE_KIND, create_type=False),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("messages", "kind")
    postgresql.ENUM(*VALUES, name=MESSAGE_KIND).drop(op.get_bind(), checkfirst=True)
