"""One row per billable request: the base for quotas and cost.

## Why a table and not a log line

A log line answers "what happened"; this answers "how much did org X
consume this month", which is a query. Metering is the foundation for
three things the product will need and cannot retrofit from logs:
per-tenant quotas, cost attribution (a RAG answer costs an embed, a
rerank and a completion, and the ratio differs wildly by tenant), and
billing. Log aggregation can be made to answer it, but only by keeping
the logs forever and parsing them, and a row is cheaper than both.

## Why org_id is NOT NULL and indexed with created_at

Every query against this table is "this org, this date range" -- the
usage endpoint, a quota check, an invoice. A composite index on
`(org_id, created_at)` serves all of them; an index on `created_at`
alone would scan every tenant's rows to answer a question about one.

The column is non-nullable per the project's first non-negotiable. A
nullable org_id here would be worse than elsewhere: an unattributed
usage row is a cost nobody pays.

## Why the token columns are nullable

They are nullable because the honest value is often unknown, and zero
is a lie. A cache hit spends no LLM tokens -- that is a real zero. A
provider that did not return a usage block spent an unknown number --
that is NULL. Summing them must not quietly treat "we do not know" as
"nothing", so the distinction is kept in the column rather than
flattened at write time.

## Why there is no FK on user_id with a cascade

`user_id` is nullable and has no ON DELETE: a usage row outlives the
user who created it, because the org was still billed for the work. A
cascade would delete a month of revenue evidence along with an offboarded
employee.
"""
import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UsageStatus(str, enum.Enum):
    """How the metered request ended.

    Kept apart from `messages.kind` even though the two overlap, because
    they answer different questions: `kind` is what the USER was shown,
    this is whether the work was BILLABLE. A refusal by the confidence
    gate is a refusal to the user and still cost an embed and a rerank;
    a rate-limited request cost nothing at all.
    """

    OK = "ok"
    # Declined on content: the gate found nothing, or the provider's
    # safety filter refused. Retrieval was still paid for.
    REFUSED = "refused"
    # Infrastructure failed mid-flight. Partially billable, and the part
    # that was spent is recorded.
    ERROR = "error"
    # Rejected before any provider call: a 429 or the budget guard.
    # Recorded so an operator can see that limits are biting, which is
    # invisible if only successful work is logged.
    REJECTED = "rejected"


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Nullable and without a cascade: see the module docstring. A usage
    # row outlives the person who caused it.
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    # The logical operation, not the URL path. "answer" rather than
    # "/api/v1/conversations/{id}/messages": the path changes when the
    # API is versioned, and a metering table that renames its own
    # dimension on a route change cannot be summed across the change.
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)

    embedding_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    llm_input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    llm_output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rerank_calls: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # A real boolean, not derived from zero tokens: a cache hit and a
    # gated refusal both spend no LLM tokens and are different events.
    cache_hit: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[UsageStatus] = mapped_column(
        SAEnum(
            UsageStatus,
            name="usage_status",
            # The ORM must send the enum VALUE, not its NAME. Without
            # this, SQLAlchemy sends 'OK' and Postgres rejects it --
            # the same bug that made every user and document insert fail
            # in the Phase 1 audit.
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        # clock_timestamp(), not now(): now() is transaction_timestamp()
        # and is constant for a whole transaction, so several events
        # written in one transaction would share a timestamp. That
        # already caused a bug in `messages` (an answer sorting above
        # its own question) and is worse here, where the column is the
        # axis of every aggregation.
        server_default=func.clock_timestamp(),
        nullable=False,
    )

    __table_args__ = (
        # Composite and in this order: every query is "one org, a date
        # range", so org_id must be the leading column. Reversed, the
        # index would be useless for exactly the queries it exists for.
        Index("ix_usage_events_org_created", "org_id", "created_at"),
    )
