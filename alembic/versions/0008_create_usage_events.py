"""create usage_events

One row per billable request. The base for quotas, cost attribution and
billing -- none of which can be reconstructed from logs after the fact
without keeping and parsing every log line forever.

The index is composite and org_id leads it, because every query against
this table is "one org, one date range". An index on created_at alone
would scan every tenant's rows to answer a question about one.

Revision ID: 0008
Revises: 0007
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

USAGE_STATUS = "usage_status"
VALUES = ("ok", "refused", "error", "rejected")


def upgrade() -> None:
    # Created explicitly, then referenced with create_type=False in
    # create_table. Passing a fresh sa.Enum into create_table emits a
    # SECOND CREATE TYPE and fails with DuplicateObject -- the bug that
    # made migrations 0001 and 0002 unrunnable on a fresh database.
    postgresql.ENUM(*VALUES, name=USAGE_STATUS).create(op.get_bind(), checkfirst=True)

    op.create_table(
        "usage_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # No ondelete: a usage row outlives the user who caused it,
        # because the org was still billed for the work. A cascade here
        # would delete revenue evidence along with an offboarded
        # employee.
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("endpoint", sa.String(length=64), nullable=False),
        # Nullable because "unknown" and "zero" are different facts: a
        # cache hit really spent no LLM tokens, while a provider that
        # returned no usage block spent an unknown number. Summing must
        # not silently read the second as the first.
        sa.Column("embedding_tokens", sa.Integer(), nullable=True),
        sa.Column("llm_input_tokens", sa.Integer(), nullable=True),
        sa.Column("llm_output_tokens", sa.Integer(), nullable=True),
        sa.Column("rerank_calls", sa.Integer(), nullable=True),
        sa.Column("cache_hit", sa.Boolean(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(*VALUES, name=USAGE_STATUS, create_type=False),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            # clock_timestamp(), not now(). now() is
            # transaction_timestamp() and is constant for a whole
            # transaction, so several events written together would
            # share a timestamp -- and this column is the axis of every
            # aggregation over this table.
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_usage_events_org_created", "usage_events", ["org_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_usage_events_org_created", table_name="usage_events")
    op.drop_table("usage_events")
    postgresql.ENUM(*VALUES, name=USAGE_STATUS).drop(op.get_bind(), checkfirst=True)
