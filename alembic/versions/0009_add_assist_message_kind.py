"""add 'assist' to the message_kind enum

Greetings and "what can you do" are answered without retrieval and
without a model call. They need a kind of their own: as 'answer' they
would be uncited answers the UI presents as grounded, and as 'refusal'
they would inflate the refusal rate on the usage dashboard.

Revision ID: 0009
Revises: 0008
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ENUM_NAME = "message_kind"
NEW_VALUE = "assist"


def upgrade() -> None:
    # IF NOT EXISTS so a re-run is harmless. Postgres 12+ permits
    # ALTER TYPE ... ADD VALUE inside a transaction as long as the new
    # value is not USED in the same transaction -- this migration only
    # declares it, so Alembic's transaction is fine.
    op.execute(f"ALTER TYPE {ENUM_NAME} ADD VALUE IF NOT EXISTS '{NEW_VALUE}'")


def downgrade() -> None:
    # Postgres cannot drop a value from an enum. Undoing this means
    # rebuilding the type, which would have to rewrite every row in
    # messages and would fail outright if any row already uses
    # 'assist'. Raising is honest: there is no safe automatic reverse,
    # and a downgrade that silently did nothing would leave the schema
    # and the migration history disagreeing.
    raise NotImplementedError(
        "Postgres cannot remove an enum value. To reverse this, rebuild "
        "message_kind by hand after confirming no row uses 'assist'."
    )
