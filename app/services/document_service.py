import logging
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.models.organization import Organization
from app.services.storage import StorageError, build_storage_key, get_storage
from app.workers.ingestion import ingest_document

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md"}
# Read size for the streaming size check. 1 MiB is a compromise: small
# enough that an oversized upload is rejected after ~1 MiB of memory,
# large enough that a 25 MiB file is not 25k syscalls.
_UPLOAD_CHUNK_BYTES = 1024 * 1024


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


async def read_upload_within_limit(file: UploadFile) -> bytes:
    """Read the upload, refusing anything over MAX_UPLOAD_BYTES.

    Counted while reading rather than trusted from Content-Length: that
    header is client-supplied, so a request can declare 1 KB and send 2 GB.
    A plain `await file.read()` would buy whatever the client sends into
    memory before any validation runs at all.
    """
    limit = settings.MAX_UPLOAD_BYTES
    parts: list[bytes] = []
    total = 0
    while True:
        part = await file.read(_UPLOAD_CHUNK_BYTES)
        if not part:
            break
        total += len(part)
        if total > limit:
            # 413 rather than 400: the request is well-formed, just too
            # large, and the client can act on the distinction.
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File exceeds the maximum upload size of {limit} bytes.",
            )
        parts.append(part)

    if total == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="The uploaded file is empty."
        )
    return b"".join(parts)


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
    # The key is built here, never accepted from the client, and the
    # filename inside it is sanitized by build_storage_key.
    storage_key = build_storage_key(
        org_id=org_id, document_id=document_id, filename=filename
    )

    # Bytes land in storage BEFORE the row commits. The opposite order
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
    # in another process and must never look up a row this transaction has
    # not made visible yet.
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


def count_searchable_chunks(db: Session, *, org_id: uuid.UUID, document_id: uuid.UUID) -> int:
    # Children only: parents are context, not search targets, so this is
    # the number that tells a user whether their document is findable.
    return (
        db.query(func.count(DocumentChunk.id))
        .filter(
            DocumentChunk.document_id == document_id,
            DocumentChunk.org_id == org_id,
            DocumentChunk.parent_id.isnot(None),
        )
        .scalar()
        or 0
    )


def delete_document(db: Session, *, org_id: uuid.UUID, document_id: uuid.UUID) -> bool:
    """Remove the storage object, the chunks and the row.

    Returns False if no such document exists in this org, so the route can
    answer 404 without a second query.
    """
    document = get_document(db, org_id=org_id, document_id=document_id)
    if document is None:
        return False

    storage_key = document.storage_key

    # DB first, storage second. If the order were reversed and the commit
    # then failed, the row would survive pointing at bytes that no longer
    # exist -- a document that lists fine and fails every ingestion. This
    # way the worst case is an orphaned object, which costs storage and
    # nothing else.
    db.query(DocumentChunk).filter(
        DocumentChunk.document_id == document_id, DocumentChunk.org_id == org_id
    ).delete(synchronize_session=False)
    db.delete(document)
    # Same transaction as the delete: the corpus just changed, so every
    # cached answer for this org has to stop being served. A cached answer
    # citing a deleted document is worse than a cache miss.
    db.execute(
        update(Organization)
        .where(Organization.id == org_id)
        .values(index_version=Organization.index_version + 1)
    )
    db.commit()

    try:
        get_storage().delete(storage_key)
    except StorageError:
        # Best-effort and deliberately non-fatal: the document is already
        # gone from the user's point of view, and raising here would make a
        # successful delete look like a failure they should retry.
        logger.warning("could not delete storage object %s", storage_key, exc_info=True)

    return True
