import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

from app.models.document import DocumentStatus


class DocumentUploadResponse(BaseModel):
    document_id: uuid.UUID
    status: DocumentStatus
    # Told to the client explicitly so it knows polling is expected rather
    # than having to infer it from the 202.
    poll_url: str


class DocumentListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    status: DocumentStatus
    file_size_bytes: int
    created_at: datetime


class DocumentDetailResponse(BaseModel):
    # An allowlist, not a filter: storage_key and org_id are absent by
    # construction. storage_key in particular must never reach a client --
    # it is the address of the object in the bucket.
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    status: DocumentStatus
    file_size_bytes: int
    created_at: datetime
    processed_at: Optional[datetime]
    error_message: Optional[str]
    chunk_count: int = 0
