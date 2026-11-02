import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

from app.models.document import DocumentStatus
from app.services.document_failures import FailureCode


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
    # NOT error_message. The raw provider exception stays in the
    # database and the logs for operators; publishing it put the
    # embedding provider, the model name and an upstream Google URL on
    # the Documents page, which is both useless to the uploader and a
    # disclosure the phase skill forbids.
    #
    # A fixed enum instead, so no provider prose can reach a response
    # through this field by construction. The sentence a person reads
    # is chosen by the UI, which is where copy belongs.
    failure_code: Optional[FailureCode] = None
    chunk_count: int = 0
