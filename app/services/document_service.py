import uuid
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.document import Document, DocumentStatus
from app.workers.ingestion import ingest_document

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md"}
UPLOAD_DIR = Path("uploads")


def _validate_extension(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type '{suffix}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
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
    suffix = _validate_extension(filename)

    document_id = uuid.uuid4()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # Stored under the document's own id, not the user-supplied filename --
    # avoids path traversal from a crafted filename and collisions between
    # unrelated uploads that happen to share a name.
    file_path = UPLOAD_DIR / f"{document_id}{suffix}"
    file_path.write_bytes(content)

    document = Document(
        id=document_id,
        org_id=org_id,
        uploaded_by=uploaded_by,
        filename=filename,
        file_path=str(file_path),
        file_size_bytes=len(content),
        status=DocumentStatus.PENDING,
    )
    db.add(document)
    # Committed before the task is enqueued, deliberately: the worker must
    # never race a document row that doesn't exist yet from its own
    # transaction's point of view.
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
