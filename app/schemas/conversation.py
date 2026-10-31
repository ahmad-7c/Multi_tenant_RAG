import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.conversation import MessageKind, MessageRole


class CreateConversationRequest(BaseModel):
    # No org_id and no user_id: both come from the verified token. Letting
    # a caller name either would be a cross-tenant escalation.
    title: Optional[str] = Field(default=None, max_length=500)


class ConversationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: Optional[str]
    created_at: datetime


class CreateMessageRequest(BaseModel):
    # Bounded: the question is embedded and sent to a model, and an
    # unbounded string is both a cost and a token-limit problem.
    content: str = Field(min_length=1, max_length=4000)


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: MessageRole
    content: str
    # Exposed so a client can tell a refusal or an infrastructure error
    # from an answer. Without it the three are one grey bubble of prose
    # and the UI has to guess from the wording -- which is exactly the
    # heuristic `kind` exists to replace. NULL for user turns and for
    # rows written before migration 0006, so it is Optional on the wire
    # too rather than defaulted to ANSWER: claiming a pre-migration
    # refusal was an answer is worse than admitting we do not know.
    kind: Optional[MessageKind]
    cited_chunk_ids: Optional[list[uuid.UUID]]
    created_at: datetime
