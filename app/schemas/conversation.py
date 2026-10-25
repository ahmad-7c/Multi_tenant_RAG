import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.conversation import MessageRole


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
    cited_chunk_ids: Optional[list[uuid.UUID]]
    created_at: datetime
