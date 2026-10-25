import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
from celery.exceptions import Retry
from openai import OpenAI

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.services.chunking import build_child_chunks, build_parent_chunks
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_BATCH_SIZE = 20
MAX_EMBEDDING_RETRIES = 3


def _extract_pages(file_path: str) -> list[tuple[int, str]]:
    """Extract text with 1-indexed page numbers. PDFs go through PyMuPDF
    page by page as specified; .txt/.md have no real notion of a "page",
    so they're treated as a single page rather than forcing PyMuPDF to
    pretend otherwise.
    """
    suffix = Path(file_path).suffix.lower()

    if suffix == ".pdf":
        pages: list[tuple[int, str]] = []
        with pymupdf.open(file_path) as pdf:
            for index, page in enumerate(pdf):
                pages.append((index + 1, page.get_text()))
        return pages

    return [(1, Path(file_path).read_text(encoding="utf-8"))]


def _embed_batch(client: OpenAI, texts: list[str]) -> list[list[float]]:
    response = client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
    return [item.embedding for item in response.data]


def _mark_failed(db, document_id: uuid.UUID, error: Exception) -> None:
    document = db.get(Document, document_id)
    if document is None:
        return
    document.status = DocumentStatus.FAILED
    # Truncated defensively -- a library/API exception string could in
    # theory run long, and this isn't an unbounded column in every DB.
    document.error_message = str(error)[:2000]
    db.commit()


@celery_app.task(
    bind=True,
    name="app.workers.ingestion.ingest_document",
    max_retries=MAX_EMBEDDING_RETRIES,
)
def ingest_document(self, document_id: str) -> None:
    # Celery's JSON serializer only round-trips a UUID as a plain string --
    # normalize once here instead of relying on implicit coercion at every
    # later use site.
    doc_uuid = uuid.UUID(document_id) if isinstance(document_id, str) else document_id

    db = SessionLocal()
    try:
        document = db.get(Document, doc_uuid)
        if document is None:
            # Row was deleted after the task was enqueued. Nothing to
            # update -- this is not a failure, just a no-op.
            logger.warning("ingest_document: document %s no longer exists", document_id)
            return

        # Idempotency: whether this is the first attempt or a retry after a
        # crash/redelivery, start from a clean slate instead of appending to
        # or reasoning about partially-written chunks from a prior attempt.
        db.query(DocumentChunk).filter(DocumentChunk.document_id == document.id).delete()
        document.status = DocumentStatus.PROCESSING
        document.error_message = None
        db.commit()

        try:
            pages = _extract_pages(document.file_path)
        except Exception as exc:  # corrupt/unreadable file
            # The problem is the input file itself -- retrying the same
            # bytes won't change the outcome, so fail immediately instead
            # of burning the embedding-call retry budget on it.
            _mark_failed(db, doc_uuid, exc)
            return

        parents = build_parent_chunks(pages)

        parent_rows = [
            DocumentChunk(
                # Generated here (not left to the column default) because
                # child rows need a parent's id as their parent_id FK
                # before any row is flushed to the DB.
                id=uuid.uuid4(),
                org_id=document.org_id,
                document_id=document.id,
                parent_id=None,
                content=parent.content,
                embedding=None,
                page_number=parent.page_number,
                token_count=parent.token_count,
                chunk_index=parent.chunk_index,
            )
            for parent in parents
        ]
        # Parents first, then children: children's parent_id depends on
        # parent ids, and inserting in this order means a failure partway
        # through children never leaves an orphaned child pointing at a
        # parent that doesn't exist.
        db.bulk_save_objects(parent_rows)
        db.flush()

        child_rows: list[DocumentChunk] = []
        for parent, parent_row in zip(parents, parent_rows):
            for child in build_child_chunks(parent):
                child_rows.append(
                    DocumentChunk(
                        id=uuid.uuid4(),
                        org_id=document.org_id,
                        document_id=document.id,
                        parent_id=parent_row.id,
                        content=child.content,
                        embedding=None,
                        page_number=child.page_number,
                        token_count=child.token_count,
                        chunk_index=child.chunk_index,
                    )
                )

        client = OpenAI(api_key=settings.OPENAI_API_KEY)

        for batch_start in range(0, len(child_rows), EMBEDDING_BATCH_SIZE):
            batch = child_rows[batch_start : batch_start + EMBEDDING_BATCH_SIZE]
            try:
                embeddings = _embed_batch(client, [row.content for row in batch])
            except Exception as exc:
                retry_count = self.request.retries
                # countdown = 5 * (2 ** retry_count) per spec: 5s, 10s, 20s.
                # self.retry() raises Retry if attempts remain (Celery
                # reschedules the whole task -- safe because the task is
                # idempotent and redoes everything from scratch), or
                # MaxRetriesExceededError once max_retries is exhausted,
                # which falls through to the catch-all below and fails the
                # document instead of leaving it stuck in "processing".
                raise self.retry(exc=exc, countdown=5 * (2**retry_count))

            for row, embedding in zip(batch, embeddings):
                row.embedding = embedding

        db.bulk_save_objects(child_rows)

        document.status = DocumentStatus.READY
        document.processed_at = datetime.now(timezone.utc)
        db.commit()

    except Retry:
        # Celery's own retry signal, not a failure -- must not be caught by
        # the catch-all below, or every retry attempt would incorrectly
        # mark the document failed instead of rescheduling.
        raise
    except Exception as exc:
        # Any other unexpected error (DB error, bug, exhausted retries via
        # MaxRetriesExceededError, etc.) must still leave the document in a
        # terminal "failed" state rather than stuck in "processing" forever.
        db.rollback()
        _mark_failed(db, doc_uuid, exc)
        raise
    finally:
        db.close()
