"""The ingestion task.

Data flow, including every failure path:

    POST /documents
        |
        | storage.save(key, bytes)   <- bytes land BEFORE the row commits
        | INSERT documents (status=pending)
        | ingest_document.delay(id)
        v
    +---------------------- Celery worker -----------------------+
    |                                                            |
    |  [tx 1]  SELECT document; status := processing             |
    |             |                                              |
    |             +-- row gone --------------------> return (no-op)
    |             v                                              |
    |  storage.read(key) ------- missing/corrupt --> [tx F] failed
    |             v                                              |
    |  extract_pages() --------- NeedsOCRError ----> [tx F] failed
    |             |              EmptyDocument                   |
    |             v                                              |
    |  clean_pages()                                             |
    |             v                                              |
    |  chunk_pages() ----------- zero chunks ------> [tx F] failed
    |             v                                              |
    |  embed_in_batches()  (NO DB SESSION HELD)                   |
    |     |         |                                            |
    |     |         +-- permanent (400) ----------> [tx F] failed |
    |     |         +-- transient, retries left --> retry batch   |
    |     |         +-- transient, exhausted -----> [tx F] failed |
    |     v                                                      |
    |  [tx 2]  ONE transaction:                                   |
    |            DELETE old chunks                                |
    |            INSERT parents                                   |
    |            INSERT children (+ embeddings)                   |
    |            status := ready, processed_at := now             |
    |            organizations.index_version += 1                 |
    |          COMMIT  <- the first moment anything is visible    |
    |                                                            |
    |  worker crash anywhere above -> message never acked        |
    |                                -> redelivered -> rerun     |
    +------------------------------------------------------------+

Two properties fall out of that shape:

  - **No half-embedded state.** Everything is embedded in memory before
    the write transaction opens, so there is no instant at which a
    document is `ready` with some children unembedded, and no instant at
    which an open write transaction is waiting on a network call.
  - **Idempotent.** The DELETE lives inside the same transaction as the
    INSERTs, so a redelivered attempt replaces the previous attempt's
    chunks atomically. Running the task twice converges on one correct
    set of chunks, never two.
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import update

from app.db.session import SessionLocal
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.models.organization import Organization
from app.services.chunking import chunk_pages
from app.services.cleaning import clean_pages
from app.services.embedding import EmbeddingError, embed_in_batches, get_embedding_provider
from app.services.extraction import ExtractionError, extract_pages
from app.services.storage import StorageError, get_storage
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Errors that mean "this document can never be ingested as-is". All of them
# are reported to the uploader via documents.error_message rather than
# retried, because the same bytes will fail the same way.
PERMANENT_ERRORS = (ExtractionError, StorageError, EmbeddingError)


class NoChunksProducedError(Exception):
    """Chunking yielded nothing.

    Guarded explicitly because the alternative -- marking the document
    READY with zero chunks -- is the worst possible outcome: the UI says
    the document is searchable and every query silently misses it.
    """


def _mark_failed(document_id: uuid.UUID, error: Exception) -> None:
    """Terminal failure, in its own short transaction.

    A separate session because the one that failed may be in an
    unusable state, and leaving the document stuck in `processing`
    forever is worse than any error message.
    """
    db = SessionLocal()
    try:
        document = db.get(Document, document_id)
        if document is None:
            return
        document.status = DocumentStatus.FAILED
        # Truncated: a provider exception string can carry an entire
        # response body, and this is shown to a user.
        document.error_message = str(error)[:2000]
        document.processed_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:  # pragma: no cover - last-resort guard
        db.rollback()
        logger.exception("could not mark document %s failed", document_id)
    finally:
        db.close()


@celery_app.task(bind=True, name="app.workers.ingestion.ingest_document")
def ingest_document(self, document_id: str) -> None:
    # Celery's JSON serializer round-trips a UUID as a plain string, so
    # normalize once here rather than coercing at every use site.
    doc_uuid = uuid.UUID(document_id) if isinstance(document_id, str) else document_id

    # ---- tx 1: claim the document -------------------------------------
    db = SessionLocal()
    try:
        document = db.get(Document, doc_uuid)
        if document is None:
            # Deleted after the task was enqueued. Not a failure -- there
            # is nothing left to mark.
            logger.warning("ingest_document: document %s no longer exists", document_id)
            return
        org_id = document.org_id
        storage_key = document.storage_key
        filename = document.filename
        document.status = DocumentStatus.PROCESSING
        document.error_message = None
        # Committed so a client polling GET /documents/{id} sees progress
        # instead of a document sitting at `pending` for minutes.
        db.commit()
    finally:
        # Closed before any slow work begins. Holding a session across
        # extraction and N embedding calls would pin a pooled connection
        # (and in the previous implementation, an open write transaction)
        # for the entire duration.
        db.close()

    # ---- no DB session held from here until tx 2 ----------------------
    try:
        raw = get_storage().read(storage_key)
        pages = extract_pages(raw, filename)
        pages = clean_pages(pages)
        parents = chunk_pages(pages)

        if not parents:
            raise NoChunksProducedError(
                "No chunks could be produced from this document. It extracted "
                "successfully but contained no usable text."
            )

        children = [(p_index, child) for p_index, p in enumerate(parents) for child in p.children]
        if not children:
            raise NoChunksProducedError(
                "Chunking produced parent chunks but no searchable child chunks."
            )

        provider = get_embedding_provider()
        # Every embedding is obtained BEFORE the write transaction opens.
        # This is the line that removes the half-embedded state: by the
        # time any row is written, either all vectors exist or none do.
        vectors = embed_in_batches(provider, [child.content for _, child in children])

        if len(vectors) != len(children):
            raise NoChunksProducedError(
                f"Embedder returned {len(vectors)} vectors for {len(children)} chunks."
            )
    except PERMANENT_ERRORS as exc:
        _mark_failed(doc_uuid, exc)
        return
    except NoChunksProducedError as exc:
        _mark_failed(doc_uuid, exc)
        return
    except Exception as exc:
        # Unexpected (a bug, an OOM, a DB blip). Still has to leave a
        # terminal state rather than `processing` forever -- then re-raise
        # so the failure is visible in Celery and the worker's logs.
        _mark_failed(doc_uuid, exc)
        raise

    # ---- tx 2: one transaction for everything -------------------------
    db = SessionLocal()
    try:
        document = db.get(Document, doc_uuid)
        if document is None:
            # Deleted while we were embedding. Nothing to write, and the
            # stored object is already gone with it.
            logger.warning("ingest_document: document %s deleted mid-ingestion", document_id)
            return

        # Idempotency lives here, inside the write transaction: a
        # redelivered attempt replaces the previous attempt's chunks
        # atomically. Doing it in tx 1 would leave a window where the
        # document has no chunks at all.
        db.query(DocumentChunk).filter(DocumentChunk.document_id == doc_uuid).delete(
            synchronize_session=False
        )

        parent_rows = []
        for parent in parents:
            parent_rows.append(
                DocumentChunk(
                    # ids generated here, not left to the column default,
                    # because children need their parent's id as an FK
                    # before anything is flushed.
                    id=uuid.uuid4(),
                    org_id=org_id,
                    document_id=doc_uuid,
                    parent_id=None,
                    content=parent.content,
                    # Parents are never searched by vector distance; they
                    # are the context handed to the LLM.
                    embedding=None,
                    embedding_model=None,
                    page_number=parent.page_number,
                    token_count=parent.token_count,
                    chunk_index=parent.chunk_index,
                )
            )
        db.add_all(parent_rows)

        child_rows = []
        for (parent_index, child), vector in zip(children, vectors):
            child_rows.append(
                DocumentChunk(
                    id=uuid.uuid4(),
                    org_id=org_id,
                    document_id=doc_uuid,
                    parent_id=parent_rows[parent_index].id,
                    content=child.content,
                    embedding=vector,
                    # Recorded per row so a future model migration can tell
                    # which vectors still need re-embedding.
                    embedding_model=provider.model,
                    page_number=child.page_number,
                    token_count=child.token_count,
                    chunk_index=child.chunk_index,
                )
            )
        db.add_all(child_rows)

        document.status = DocumentStatus.READY
        document.processed_at = datetime.now(timezone.utc)
        document.error_message = None

        # Bumped in the SAME transaction that makes the chunks visible, so
        # a cached answer can never be served from an index_version that
        # predates content already searchable. An UPDATE ... + 1 rather
        # than read-modify-write, so two documents finishing concurrently
        # both count.
        db.execute(
            update(Organization)
            .where(Organization.id == org_id)
            .values(index_version=Organization.index_version + 1)
        )

        db.commit()
        logger.info(
            "ingested document %s: %s parents, %s children",
            document_id,
            len(parent_rows),
            len(child_rows),
        )
    except Exception as exc:
        db.rollback()
        db.close()
        _mark_failed(doc_uuid, exc)
        raise
    finally:
        db.close()
