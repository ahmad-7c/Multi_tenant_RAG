import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class MessageRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"


class MessageKind(str, enum.Enum):
    """What an assistant turn actually is.

    NULL on user turns -- a question is neither an answer nor a refusal,
    and inventing a fourth member for it would make every filter say
    "kind == ANSWER or role == USER" instead of just reading the column.

    The distinction that matters downstream is ANSWER vs everything
    else: only a real grounded answer is useful context for rewriting a
    follow-up. REFUSAL and ERROR are kept apart from each other because
    they mean different things to an operator reading the table --
    refusals are a product signal (the corpus or the gate), errors are
    an infrastructure signal.
    """

    # A grounded answer the model actually produced.
    ANSWER = "answer"
    # The system declined on CONTENT grounds: the confidence gate found
    # nothing relevant, or the provider's safety filter refused.
    REFUSAL = "refusal"
    # Infrastructure failed: rate limit, timeout, empty response.
    ERROR = "error"
    # A conversational turn that was never a retrieval question: a
    # greeting, "what can you do", "thanks". Answered deterministically
    # with no model call and no retrieval.
    #
    # Its own member rather than reusing ANSWER or REFUSAL, because
    # both would corrupt a number someone reads. As ANSWER it would be
    # an uncited "answer" the UI presents as grounded, and it would
    # enter the rewrite window as context. As REFUSAL it would inflate
    # the refusal rate on the usage dashboard -- turning "say hello
    # more often" into "quality is dropping".
    ASSIST = "assist"


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        # Listing a user's conversations is the only read pattern, and it
        # is always scoped to both.
        Index("ix_conversations_org_user", "org_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    # Conversations are private to the user who started them, not shared
    # across the org: a question can reveal what someone was looking into.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        # Reading a conversation always means "this conversation, in
        # order", so the index carries the sort column.
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Denormalized from conversations.org_id for the same reason
    # document_chunks carries it: every query on tenant data filters by
    # org_id, and that filter should not depend on a join.
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    role: Mapped[MessageRole] = mapped_column(
        SAEnum(
            MessageRole,
            name="message_role",
            # See User.role: without values_callable SQLAlchemy persists
            # the enum's NAME and Postgres rejects it.
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Nullable, and NULL means two different things that happen to want
    # the same treatment: a user turn (kind does not apply) and a row
    # written before this column existed (a legacy answer). Both are fed
    # to the rewriter, which is why the filter tests for "not a refusal
    # or error" rather than for "== ANSWER".
    kind: Mapped[Optional[MessageKind]] = mapped_column(
        SAEnum(
            MessageKind,
            name="message_kind",
            # See MessageRole: without values_callable SQLAlchemy
            # persists the enum's NAME and Postgres rejects it.
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=True,
    )

    # The parent chunks actually cited by this answer. Stored as an array
    # rather than a join table: it is only ever read back whole with the
    # message, never queried across messages, so a join table would add a
    # table and a join for no query it would serve.
    cited_chunk_ids: Mapped[Optional[list[uuid.UUID]]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=True
    )

    # clock_timestamp(), not now(). now() is transaction_timestamp() and
    # is CONSTANT for a whole transaction -- and persist_turn writes the
    # question and the answer in one. Both rows would share a timestamp
    # and nothing in the ordering could separate them. See migration 0007.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
