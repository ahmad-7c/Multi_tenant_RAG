"""messages.created_at: now() -> clock_timestamp()

Postgres now() is transaction_timestamp(): it returns the instant the
TRANSACTION began and is constant for its whole duration. persist_turn
writes the user message and the assistant message and commits once, so
both rows were landing with a byte-identical created_at and nothing in
the ordering could separate them.

clock_timestamp() is the actual wall clock at the moment the row is
inserted, so two inserts in one transaction get distinct values and
"question before answer" holds on the timestamp alone.

The application ORDER BY also carries an explicit role tie-break (see
conversation_service._TURN_ORDER) rather than relying on this default,
because rows written before this migration still share a timestamp and
must still sort correctly.

Revision ID: 0007
Revises: 0006
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "messages",
        "created_at",
        server_default=sa.text("clock_timestamp()"),
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "messages",
        "created_at",
        server_default=sa.text("now()"),
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
    )
