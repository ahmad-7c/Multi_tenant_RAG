import uuid
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.document import Document, DocumentStatus
from app.services.storage import build_storage_key, get_storage
from app.workers.ingestion import ingest_document

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md"}


def _validate_extension(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported file type '{suffix}'. "
                f"Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
            ),
        )
    return suffix


def create_document(
    db: Session,
    *,
    org_id: uuid.UUID,
    uploaded_by: uuid.UUID,
    filename: str,
    content: bytes,
) -> Document:
    _validate_extension(filename)

    document_id = uuid.uuid4()
    # The key is built, never taken from the client, and the filename
    # inside it is sanitized by build_storage_key.
    storage_key = build_storage_key(
        org_id=org_id, document_id=document_id, filename=filename
    )

    # Bytes land in storage BEFORE the row is committed. The opposite order
    # would let the worker pick up a pending document whose object does not
    # exist yet and fail it as unreadable. A stored object with no row is
    # merely garbage; a row with no object is a broken document.
    get_storage().save(storage_key, content)

    document = Document(
        id=document_id,
        org_id=org_id,
        uploaded_by=uploaded_by,
        filename=filename,
        storage_key=storage_key,
        file_size_bytes=len(content),
        status=DocumentStatus.PENDING,
    )
    db.add(document)
    # Committed before the task is enqueued, deliberately: the worker runs
    # in another process and must never look up a row that this
    # transaction has not made visible yet.
    db.commit()

    ingest_document.delay(str(document.id))

    return document


def list_documents(db: Session, *, org_id: uuid.UUID) -> list[Document]:
    return (
        db.query(Document)
        .filter(Document.org_id == org_id)
        .order_by(Document.created_at.desc())
        .all()
    )


def get_document(db: Session, *, org_id: uuid.UUID, document_id: uuid.UUID) -> Document | None:
    # org_id is part of the query itself, not a post-fetch check: a
    # cross-org id simply matches no row, rather than matching a row that
    # then gets rejected after the fact.
    return (
        db.query(Document)
        .filter(Document.id == document_id, Document.org_id == org_id)
        .first()
    )
