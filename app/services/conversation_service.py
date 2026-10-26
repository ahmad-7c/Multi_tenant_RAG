import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.conversation import Conversation, Message, MessageKind, MessageRole

# Within one turn the user message and the assistant message are written
# in the SAME transaction, and Postgres now() is transaction_timestamp()
# -- constant for the whole transaction. So both rows carry an identical
# created_at and the sort falls through to whatever comes next.
#
# "next" used to be Message.role, which is not a tie-break but an
# alphabetical sort: 'assistant' < 'user'. The transcript therefore
# rendered the answer ABOVE the question that produced it. This puts the
# question first explicitly instead of relying on how the enum happens
# to spell itself.
_TURN_ORDER = sa.case((Message.role == MessageRole.USER, 0), else_=1)


def create_conversation(
    db: Session, *, org_id: uuid.UUID, user_id: uuid.UUID, title: str | None
) -> Conversation:
    conversation = Conversation(
        id=uuid.uuid4(), org_id=org_id, user_id=user_id, title=title
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def list_conversations(
    db: Session, *, org_id: uuid.UUID, user_id: uuid.UUID
) -> list[Conversation]:
    # Scoped to org AND user: conversations are private to whoever started
    # them, because a question reveals what someone was looking into.
    return (
        db.query(Conversation)
        .filter(Conversation.org_id == org_id, Conversation.user_id == user_id)
        .order_by(Conversation.created_at.desc())
        .all()
    )


def get_conversation(
    db: Session, *, org_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation | None:
    # Both filters are part of the query, not a post-fetch check, so
    # another org's (or another user's) id simply matches no row.
    return (
        db.query(Conversation)
        .filter(
            Conversation.id == conversation_id,
            Conversation.org_id == org_id,
            Conversation.user_id == user_id,
        )
        .first()
    )


def list_messages(
    db: Session, *, org_id: uuid.UUID, conversation_id: uuid.UUID
) -> list[Message]:
    return (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id, Message.org_id == org_id)
        .order_by(Message.created_at.asc(), _TURN_ORDER.asc())
        .all()
    )


def history_for_rewrite(
    db: Session, *, org_id: uuid.UUID, conversation_id: uuid.UUID, turns: int
) -> list[dict]:
    """Recent turns as plain dicts, answers only.

    Plain dicts, not ORM objects, because this is consumed after the
    session closes -- an ORM instance would lazy-load and reopen a
    connection mid-stream.

    Refusals and errors are EXCLUDED. They are real messages the user
    saw -- list_messages still returns them -- but they are not context.
    A refusal in the rewrite window reads to the rewriter as the most
    recent assistant topic, so "what about contractors?" comes back as
    "what about contractors and the content safety filter?", and that
    string becomes both the retrieval query and the cache key. One
    refusal would degrade every later turn in the conversation.

    The filter is "not a refusal or error" rather than "== ANSWER" so
    that NULL rows pass: NULL is both a user turn and every assistant
    row written before the column existed.
    """
    excluded = (MessageKind.REFUSAL, MessageKind.ERROR)
    messages = (
        db.query(Message)
        .filter(
            Message.conversation_id == conversation_id,
            Message.org_id == org_id,
            # The exclusion is applied BEFORE the limit. Filtering after
            # would let four refused turns in a row empty the window
            # while real answers sit just above them.
            sa.or_(Message.kind.is_(None), Message.kind.notin_(excluded)),
        )
        # Descending to take the most RECENT turns, then reversed below.
        # The tie-break reverses with it, so a turn written in one
        # transaction still comes back question-then-answer.
        .order_by(Message.created_at.desc(), _TURN_ORDER.desc())
        .limit(turns * 2)
        .all()
    )
    return [
        {"role": message.role.value, "content": message.content}
        for message in reversed(messages)
    ]
