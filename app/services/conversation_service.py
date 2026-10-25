import uuid

from sqlalchemy.orm import Session

from app.models.conversation import Conversation, Message


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
        .order_by(Message.created_at.asc(), Message.role.asc())
        .all()
    )


def history_for_rewrite(
    db: Session, *, org_id: uuid.UUID, conversation_id: uuid.UUID, turns: int
) -> list[dict]:
    """Recent turns as plain dicts.

    Plain dicts, not ORM objects, because this is consumed after the
    session closes -- an ORM instance would lazy-load and reopen a
    connection mid-stream.
    """
    messages = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id, Message.org_id == org_id)
        .order_by(Message.created_at.desc())
        .limit(turns * 2)
        .all()
    )
    return [
        {"role": message.role.value, "content": message.content}
        for message in reversed(messages)
    ]
