"""Ingestion task tests.

Run against real Postgres with the fake embedder and the local storage
backend: no network, no Celery broker (the task is called synchronously as
a plain function, which is what `.apply()`-free direct invocation gives us).
"""
import uuid

import pymupdf
import pytest

from app.core.config import settings
from app.models.document import Document, DocumentStatus
from app.models.document_chunk import DocumentChunk
from app.models.organization import Organization
from app.services import embedding as embedding_module
from app.services.storage import build_storage_key, get_storage
from app.workers import ingestion as ingestion_module
from app.workers.ingestion import ingest_document


@pytest.fixture()
def ingest_env(db_session, monkeypatch, tmp_path):
    """Point the task at this test's transaction, storage and fake embedder.

    The task opens its own sessions via SessionLocal, so SessionLocal is
    replaced with a factory returning the test's session. close() is
    neutralised because the task closes its session several times by
    design and the fixture still needs it afterwards.
    """
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
    monkeypatch.setattr(settings, "STORAGE_LOCAL_ROOT", str(tmp_path / "storage"))
    get_storage.cache_clear()

    provider = embedding_module.FakeEmbeddings(dimensions=settings.EMBEDDING_DIMENSIONS)
    monkeypatch.setattr(
        embedding_module, "get_embedding_provider", lambda: provider, raising=True
    )
    monkeypatch.setattr(ingestion_module, "get_embedding_provider", lambda: provider)

    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(ingestion_module, "SessionLocal", lambda: db_session)

    yield provider
    get_storage().__class__  # keep a reference; no teardown needed
    get_storage.cache_clear()


def _make_document(db_session, org, user, *, filename: str, data: bytes) -> Document:
    document_id = uuid.uuid4()
    key = build_storage_key(org_id=org.id, document_id=document_id, filename=filename)
    get_storage().save(key, data)
    document = Document(
        id=document_id,
        org_id=org.id,
        uploaded_by=user.id,
        filename=filename,
        storage_key=key,
        file_size_bytes=len(data),
        status=DocumentStatus.PENDING,
    )
    db_session.add(document)
    db_session.commit()
    return document


def _chunks(db_session, document_id):
    return (
        db_session.query(DocumentChunk)
        .filter(DocumentChunk.document_id == document_id)
        .all()
    )


SAMPLE_MD = (
    "# Expense Policy\n\n"
    "Employees must submit receipts within thirty days of travel. "
    "Late submissions require written approval from a director. "
    "Reimbursement is paid in the next payroll run.\n\n"
    "## Limits\n\n"
    "| Category | Limit |\n"
    "| --- | --- |\n"
    "| Meals | 60 |\n"
    "| Hotel | 250 |\n\n"
    "```python\n"
    "def reimbursable(amount, limit):\n"
    "    return amount <= limit\n"
    "```\n"
)


def _scanned_pdf(pages: int = 3) -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


# -------------------------------------------------------------- happy path ----


def test_document_becomes_ready_with_parents_and_children(
    db_session, org_factory, user_factory, ingest_env
):
    org = org_factory()
    user = user_factory(org)
    document = _make_document(db_session, org, user, filename="policy.md", data=SAMPLE_MD.encode())

    ingest_document(str(document.id))

    db_session.refresh(document)
    assert document.status == DocumentStatus.READY
    assert document.processed_at is not None
    assert document.error_message is None

    chunks = _chunks(db_session, document.id)
    parents = [c for c in chunks if c.parent_id is None]
    children = [c for c in chunks if c.parent_id is not None]
    assert parents and children


def test_parents_never_have_embeddings_and_children_always_do(
    db_session, org_factory, user_factory, ingest_env
):
    org = org_factory()
    user = user_factory(org)
    document = _make_document(db_session, org, user, filename="policy.md", data=SAMPLE_MD.encode())

    ingest_document(str(document.id))

    for chunk in _chunks(db_session, document.id):
        if chunk.parent_id is None:
            # Parents are context for the LLM, never search targets.
            assert chunk.embedding is None
            assert chunk.embedding_model is None
        else:
            assert chunk.embedding is not None
            assert len(chunk.embedding) == settings.EMBEDDING_DIMENSIONS
            # Recorded per row so a model migration knows what to re-embed.
            assert chunk.embedding_model == ingest_env.model


def test_every_child_points_at_a_real_parent_in_the_same_org(
    db_session, org_factory, user_factory, ingest_env
):
    org = org_factory()
    user = user_factory(org)
    document = _make_document(db_session, org, user, filename="policy.md", data=SAMPLE_MD.encode())

    ingest_document(str(document.id))

    chunks = _chunks(db_session, document.id)
    parent_ids = {c.id for c in chunks if c.parent_id is None}
    children = [c for c in chunks if c.parent_id is not None]

    assert children
    for child in children:
        # An orphaned child is retrievable but un-expandable: retrieval
        # finds it and then has no parent to send to the LLM.
        assert child.parent_id in parent_ids
        assert child.org_id == org.id


def test_table_and_code_block_are_not_split_across_parents(
    db_session, org_factory, user_factory, ingest_env
):
    org = org_factory()
    user = user_factory(org)
    document = _make_document(db_session, org, user, filename="policy.md", data=SAMPLE_MD.encode())

    ingest_document(str(document.id))

    parents = [c for c in _chunks(db_session, document.id) if c.parent_id is None]
    assert len([p for p in parents if "| Meals | 60 |" in p.content]) == 1
    assert len([p for p in parents if "def reimbursable" in p.content]) == 1
    for parent in parents:
        assert parent.content.count("```") % 2 == 0


def test_index_version_is_incremented_on_ready(
    db_session, org_factory, user_factory, ingest_env
):
    org = org_factory()
    user = user_factory(org)
    before = db_session.get(Organization, org.id).index_version
    document = _make_document(db_session, org, user, filename="policy.md", data=SAMPLE_MD.encode())

    ingest_document(str(document.id))

    db_session.expire_all()
    # Bumped in the same transaction that makes the chunks visible, so a
    # cached answer can never be served against an index_version that
    # predates searchable content.
    assert db_session.get(Organization, org.id).index_version == before + 1


# ------------------------------------------------------------- idempotency ----


def test_rerunning_the_task_yields_the_same_chunk_count(
    db_session, org_factory, user_factory, ingest_env
):
    org = org_factory()
    user = user_factory(org)
    document = _make_document(db_session, org, user, filename="policy.md", data=SAMPLE_MD.encode())

    ingest_document(str(document.id))
    first = _chunks(db_session, document.id)
    first_contents = sorted(c.content for c in first)

    ingest_document(str(document.id))
    second = _chunks(db_session, document.id)

    # A redelivered Celery message re-runs the whole task. Without the
    # delete-inside-the-write-transaction, this would double every chunk.
    assert len(second) == len(first)
    assert sorted(c.content for c in second) == first_contents


def test_rerunning_bumps_index_version_again(
    db_session, org_factory, user_factory, ingest_env
):
    org = org_factory()
    user = user_factory(org)
    document = _make_document(db_session, org, user, filename="policy.md", data=SAMPLE_MD.encode())

    ingest_document(str(document.id))
    db_session.expire_all()
    after_first = db_session.get(Organization, org.id).index_version

    ingest_document(str(document.id))
    db_session.expire_all()

    # Chunk ids change on a re-ingest even though content does not, so any
    # cached answer citing the old ids must be invalidated.
    assert db_session.get(Organization, org.id).index_version == after_first + 1


# ---------------------------------------------------------------- failures ----


def test_scanned_pdf_fails_with_a_needs_ocr_message(
    db_session, org_factory, user_factory, ingest_env
):
    org = org_factory()
    user = user_factory(org)
    document = _make_document(db_session, org, user, filename="scan.pdf", data=_scanned_pdf())

    ingest_document(str(document.id))

    db_session.refresh(document)
    assert document.status == DocumentStatus.FAILED
    # The uploader only ever sees error_message, so it has to say what to
    # do next.
    assert "OCR" in document.error_message
    assert _chunks(db_session, document.id) == []


def test_missing_storage_object_fails_the_document(
    db_session, org_factory, user_factory, ingest_env
):
    org = org_factory()
    user = user_factory(org)
    document = _make_document(db_session, org, user, filename="policy.md", data=SAMPLE_MD.encode())
    get_storage().delete(document.storage_key)

    ingest_document(str(document.id))

    db_session.refresh(document)
    assert document.status == DocumentStatus.FAILED
    assert document.error_message


def test_a_document_is_never_ready_with_zero_chunks(
    db_session, org_factory, user_factory, ingest_env
):
    org = org_factory()
    user = user_factory(org)
    # Punctuation only: extracts fine, chunks to nothing usable.
    document = _make_document(db_session, org, user, filename="junk.txt", data=b".  \n\n . \n")

    ingest_document(str(document.id))

    db_session.refresh(document)
    # READY with no chunks is the worst outcome available: the UI claims
    # the document is searchable and every query silently misses it.
    assert document.status == DocumentStatus.FAILED
    assert _chunks(db_session, document.id) == []


def test_permanent_embedding_error_fails_the_document_without_retrying(
    db_session, org_factory, user_factory, ingest_env, monkeypatch
):
    org = org_factory()
    user = user_factory(org)
    document = _make_document(db_session, org, user, filename="policy.md", data=SAMPLE_MD.encode())

    calls = {"n": 0}

    def boom(texts):
        calls["n"] += 1
        raise embedding_module.PermanentEmbeddingError("400 input too long")

    monkeypatch.setattr(ingest_env, "embed", boom)

    ingest_document(str(document.id))

    db_session.refresh(document)
    assert document.status == DocumentStatus.FAILED
    assert "400" in document.error_message
    assert calls["n"] == 1, "a 400 must not be retried"
    assert _chunks(db_session, document.id) == []


def test_embedding_failure_leaves_no_partial_chunks(
    db_session, org_factory, user_factory, ingest_env, monkeypatch
):
    """The half-embedded state must not be reachable.

    Embedding happens entirely in memory before the write transaction
    opens, so a failure on any batch means zero rows were written -- not
    "the first three batches are in the table".
    """
    org = org_factory()
    user = user_factory(org)
    document = _make_document(db_session, org, user, filename="policy.md", data=SAMPLE_MD.encode())

    original = ingest_env.embed
    state = {"n": 0}

    def fail_on_second_batch(texts):
        state["n"] += 1
        if state["n"] == 2:
            raise embedding_module.PermanentEmbeddingError("503-then-exhausted")
        return original(texts)

    monkeypatch.setattr(ingest_env, "embed", fail_on_second_batch)
    monkeypatch.setattr(settings, "EMBED_BATCH_SIZE", 2)

    ingest_document(str(document.id))

    db_session.refresh(document)
    assert document.status == DocumentStatus.FAILED
    assert _chunks(db_session, document.id) == []


def test_index_version_is_not_bumped_on_failure(
    db_session, org_factory, user_factory, ingest_env
):
    org = org_factory()
    user = user_factory(org)
    before = db_session.get(Organization, org.id).index_version
    document = _make_document(db_session, org, user, filename="scan.pdf", data=_scanned_pdf())

    ingest_document(str(document.id))

    db_session.expire_all()
    # Nothing became searchable, so nothing should invalidate the cache.
    assert db_session.get(Organization, org.id).index_version == before


def test_deleted_document_is_a_noop_not_a_failure(db_session, ingest_env):
    # The row was removed after the task was enqueued. There is nothing to
    # mark, and raising would make Celery retry a document that no longer
    # exists.
    ingest_document(str(uuid.uuid4()))


def test_status_moves_through_processing(
    db_session, org_factory, user_factory, ingest_env, monkeypatch
):
    org = org_factory()
    user = user_factory(org)
    document = _make_document(db_session, org, user, filename="policy.md", data=SAMPLE_MD.encode())
    seen = []

    original = ingestion_module.extract_pages

    def record(*args, **kwargs):
        # By the time extraction runs, the status commit from tx 1 has
        # already happened -- which is what lets a client polling
        # GET /documents/{id} see progress instead of `pending`.
        seen.append(db_session.get(Document, document.id).status)
        return original(*args, **kwargs)

    monkeypatch.setattr(ingestion_module, "extract_pages", record)

    ingest_document(str(document.id))

    assert seen == [DocumentStatus.PROCESSING]
